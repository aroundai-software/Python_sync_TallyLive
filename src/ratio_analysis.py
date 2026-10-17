"""
Fetch Tally Ratio Analysis for Indian FY start → today.
Stage into local SQLite first, then push to Supabase (same pattern as other sync data).
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

from .config import load_config
from .local_db import push_local_table_to_supabase, save_to_local_db
from .source_values import decode_response
from .tally_timeouts import tally_http_timeout

logger = logging.getLogger(__name__)


def indian_fy_start(on: Optional[date] = None) -> date:
    on = on or date.today()
    if on.month >= 4:
        return date(on.year, 4, 1)
    return date(on.year - 1, 4, 1)


def _fmt_tally_date(d: date) -> str:
    return d.strftime("%d-%b-%Y")


def _parse_recv_days(raw: str) -> Optional[float]:
    if not raw:
        return None
    m = re.search(r"([\d,.]+)", str(raw).replace(",", ""))
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def fetch_ratio_analysis(
    company_name: str,
    from_date: Optional[date] = None,
    to_date: Optional[date] = None,
) -> dict[str, Any]:
    """
    Export Ratio Analysis from Tally.
    Default period: Indian FY start → today.
    """
    to_date = to_date or date.today()
    from_date = from_date or indian_fy_start(to_date)
    config = load_config()
    esc = escape(company_name)
    xml = f"""<ENVELOPE>
<HEADER><VERSION>1</VERSION><TALLYREQUEST>Export</TALLYREQUEST>
<TYPE>Data</TYPE><ID>Ratio Analysis</ID></HEADER>
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
            re.sub(r"\s+", " ", (m.group(1) if m else "Ratio Analysis failed")).strip()
        )

    pairs = re.findall(
        r"<RATIONAME>(.*?)</RATIONAME>\s*<RATIOVALUE>(.*?)</RATIOVALUE>",
        text,
        re.S,
    )
    ratios: dict[str, str] = {}
    for name, value in pairs:
        ratios[name.strip()] = value.strip()

    recv_raw = ratios.get("Recv. Turnover in days", "")
    return {
        "from_date": from_date,
        "to_date": to_date,
        "ratios": ratios,
        "recv_turnover_days": _parse_recv_days(recv_raw),
        "recv_turnover_raw": recv_raw,
    }


def stage_ratio_analysis_local(
    company_name: str,
    company_guid: str,
    payload: dict[str, Any],
) -> int:
    """Write Ratio Analysis snapshot into local SQLite as pending."""
    guid = (company_guid or "").strip() or company_name
    now = datetime.now(timezone.utc).isoformat()
    record = {
        "guid": guid,
        "company_name": company_name,
        "company_guid": guid,
        "from_date": payload["from_date"].isoformat(),
        "to_date": payload["to_date"].isoformat(),
        "recv_turnover_days": payload.get("recv_turnover_days"),
        "ratios": json.dumps(payload.get("ratios") or {}, ensure_ascii=False),
        "synced_at": now,
        "updated_at": now,
    }
    return save_to_local_db("tally_ratio_analysis", [record], company_name)


def push_ratio_analysis_to_supabase(company_name: str) -> tuple:
    """Push pending local Ratio Analysis rows to Supabase."""
    config = load_config()
    supabase = create_client(config["supabase_url"], config["supabase_key"])
    return push_local_table_to_supabase(
        "tally_ratio_analysis", supabase, company_name=company_name
    )


def sync_ratio_analysis(
    company_name: str,
    company_guid: str = "",
) -> dict[str, Any]:
    """Fetch FY→today Ratio Analysis → local DB → Supabase."""
    payload = fetch_ratio_analysis(company_name)
    staged = stage_ratio_analysis_local(company_name, company_guid, payload)
    if staged < 1:
        raise RuntimeError("Failed to stage tally_ratio_analysis to local DB")

    pushed, push_err = push_ratio_analysis_to_supabase(company_name)
    days = payload.get("recv_turnover_days")
    logger.info(
        f"Ratio Analysis synced for {company_name}: "
        f"Recv. Turnover={days} days "
        f"({payload['from_date']} → {payload['to_date']}); "
        f"local=1 cloud={pushed}"
        + (f" push_err={push_err}" if push_err else "")
    )
    result = {
        "status": "ok" if not push_err else "partial",
        "recv_turnover_days": days,
        "from_date": payload["from_date"].isoformat(),
        "to_date": payload["to_date"].isoformat(),
        "staged_local": True,
        "pushed": pushed or 0,
    }
    if push_err:
        result["push_error"] = push_err
    return result
