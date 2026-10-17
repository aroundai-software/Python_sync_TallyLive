"""
Fetch Tally Cash Flow (monthly Inflow / Outflow / Nett) for Indian FY start → today.
Stage into local SQLite first, then push to Supabase (same pattern as Ratio Analysis).
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, timezone
from typing import Any, Optional
from xml.sax.saxutils import escape

import requests
from supabase import create_client

from .cash_flow_summary import attach_summaries_to_months
from .config import load_config
from .local_db import push_local_table_to_supabase, save_to_local_db
from .ratio_analysis import indian_fy_start
from .source_values import decode_response
from .tally_timeouts import tally_http_timeout

logger = logging.getLogger(__name__)


def _fmt_tally_date(d: date) -> str:
    return d.strftime("%d-%b-%Y")


def _parse_amount(raw: Optional[str]) -> float:
    if raw is None:
        return 0.0
    text = str(raw).strip().replace(",", "")
    if not text:
        return 0.0
    try:
        return float(text)
    except ValueError:
        return 0.0


def parse_cash_flow_xml(text: str) -> list[dict[str, Any]]:
    """
    Parse monthly Cash Flow export.
    Tally signs: DSPDRAMTA (often negative) = Inflow magnitude,
    DSPCRAMTA = Outflow, Nett = Inflow - Outflow.
    """
    blocks = re.findall(
        r"<DSPPERIOD>(.*?)</DSPPERIOD>\s*<DSPACCINFO>(.*?)</DSPACCINFO>",
        text,
        re.S | re.I,
    )
    months: list[dict[str, Any]] = []
    for period, body in blocks:
        label = re.sub(r"\s+", " ", period).strip()
        if not label:
            continue
        if label.lower() in ("grand total", "total"):
            continue
        dr = re.search(r"<DSPDRAMTA>(.*?)</DSPDRAMTA>", body, re.I | re.S)
        cr = re.search(r"<DSPCRAMTA>(.*?)</DSPCRAMTA>", body, re.I | re.S)
        inflow = abs(_parse_amount(dr.group(1) if dr else None))
        outflow = abs(_parse_amount(cr.group(1) if cr else None))
        nett = round(inflow - outflow, 2)
        # Skip fully empty months (Tally often pads the FY with zeros).
        if inflow == 0 and outflow == 0:
            continue
        months.append(
            {
                "period": label,
                "inflow": round(inflow, 2),
                "outflow": round(outflow, 2),
                "nett": nett,
            }
        )
    return months


def fetch_cash_flow(
    company_name: str,
    from_date: Optional[date] = None,
    to_date: Optional[date] = None,
) -> dict[str, Any]:
    """Export monthly Cash Flow from Tally. Default: Indian FY start → today."""
    to_date = to_date or date.today()
    from_date = from_date or indian_fy_start(to_date)
    config = load_config()
    esc = escape(company_name)
    xml = f"""<ENVELOPE>
<HEADER><VERSION>1</VERSION><TALLYREQUEST>Export</TALLYREQUEST>
<TYPE>Data</TYPE><ID>Cash Flow</ID></HEADER>
<BODY><DESC><STATICVARIABLES>
<SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
<SVCURRENTCOMPANY>{esc}</SVCURRENTCOMPANY>
<SVFROMDATE TYPE="Date">{_fmt_tally_date(from_date)}</SVFROMDATE>
<SVTODATE TYPE="Date">{_fmt_tally_date(to_date)}</SVTODATE>
</STATICVARIABLES></DESC></BODY>
</ENVELOPE>"""
    timeout = tally_http_timeout(local_read=60, remote_read=120)
    response = requests.post(
        config["tally_url"],
        data=xml.encode("utf-8"),
        headers={"Content-Type": "application/xml", "Connection": "close"},
        timeout=timeout,
    )
    response.raise_for_status()
    text = decode_response(response)
    if "LINEERROR" in text:
        m = re.search(r"<LINEERROR>(.*?)</LINEERROR>", text, re.I | re.S)
        raise RuntimeError(
            re.sub(r"\s+", " ", (m.group(1) if m else "Cash Flow failed")).strip()
        )

    months = parse_cash_flow_xml(text)
    # Attach per-month Cash Flow Summary (categories) from local daybook.
    months = attach_summaries_to_months(company_name, months, from_date, to_date)
    total_inflow = round(sum(m["inflow"] for m in months), 2)
    total_outflow = round(sum(m["outflow"] for m in months), 2)
    total_nett = round(total_inflow - total_outflow, 2)
    return {
        "from_date": from_date,
        "to_date": to_date,
        "months": months,
        "total_inflow": total_inflow,
        "total_outflow": total_outflow,
        "total_nett": total_nett,
    }


def stage_cash_flow_local(
    company_name: str,
    company_guid: str,
    payload: dict[str, Any],
) -> int:
    """Write Cash Flow snapshot into local SQLite as pending."""
    guid = (company_guid or "").strip() or company_name
    now = datetime.now(timezone.utc).isoformat()
    record = {
        "guid": guid,
        "company_name": company_name,
        "company_guid": guid,
        "from_date": payload["from_date"].isoformat(),
        "to_date": payload["to_date"].isoformat(),
        "months": json.dumps(payload.get("months") or [], ensure_ascii=False),
        "total_inflow": payload.get("total_inflow"),
        "total_outflow": payload.get("total_outflow"),
        "total_nett": payload.get("total_nett"),
        "synced_at": now,
        "updated_at": now,
    }
    return save_to_local_db("tally_cash_flow", [record], company_name)


def push_cash_flow_to_supabase(company_name: str) -> tuple:
    """Push pending local Cash Flow rows to Supabase."""
    config = load_config()
    supabase = create_client(config["supabase_url"], config["supabase_key"])
    return push_local_table_to_supabase(
        "tally_cash_flow", supabase, company_name=company_name
    )


def sync_cash_flow(
    company_name: str,
    company_guid: str = "",
) -> dict[str, Any]:
    """Fetch FY→today Cash Flow → local DB → Supabase."""
    payload = fetch_cash_flow(company_name)
    staged = stage_cash_flow_local(company_name, company_guid, payload)
    if staged < 1:
        raise RuntimeError("Failed to stage tally_cash_flow to local DB")

    pushed, push_err = push_cash_flow_to_supabase(company_name)
    logger.info(
        f"Cash Flow synced for {company_name}: "
        f"{len(payload.get('months') or [])} months "
        f"({payload['from_date']} → {payload['to_date']}); "
        f"nett={payload.get('total_nett')}; "
        f"local=1 cloud={pushed}"
        + (f" push_err={push_err}" if push_err else "")
    )
    result = {
        "status": "ok" if not push_err else "partial",
        "month_count": len(payload.get("months") or []),
        "total_inflow": payload.get("total_inflow"),
        "total_outflow": payload.get("total_outflow"),
        "total_nett": payload.get("total_nett"),
        "from_date": payload["from_date"].isoformat(),
        "to_date": payload["to_date"].isoformat(),
        "staged_local": True,
        "pushed": pushed or 0,
    }
    if push_err:
        result["push_error"] = push_err
    return result
