"""
Fetch Tally Ratio Analysis for Indian FY start → today.
Stage into local SQLite first, then push to Supabase (same pattern as other sync data).
"""

from __future__ import annotations

import json
import logging
import re
import time
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


def indian_fy_start(on: Optional[date] = None, books_from_iso: Optional[str] = None) -> date:
    from .date_utils import company_period_start
    return company_period_start(on, books_from_iso)


def indian_fy_bounds(
    on: Optional[date] = None,
    books_from_iso: Optional[str] = None,
    company_name: Optional[str] = None,
):
    from .date_utils import company_period_bounds
    return company_period_bounds(on, books_from_iso, company_name)


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
    books_from_iso: str = "",
) -> dict[str, Any]:
    """
    Export Ratio Analysis from Tally.
    Default period: books_from → min(today, that FY end).
    """
    if from_date is None or to_date is None:
        start, end = indian_fy_bounds(
            to_date or date.today(), books_from_iso, company_name
        )
        from_date = from_date or start
        to_date = to_date or end
    else:
        _, fy_end = indian_fy_bounds(to_date, books_from_iso, company_name)
        if to_date > fy_end:
            to_date = fy_end
        if to_date < from_date:
            to_date = from_date
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
    # Local used to be a single 60s try. Busy or Educational Tally often needs
    # longer, and "could not be setup" clears on a second attempt.
    timeout = tally_http_timeout(local_read=180, remote_read=180)
    retries = 2
    last_err: Optional[Exception] = None
    text = ""
    for attempt in range(1, retries + 1):
        try:
            logger.info(
                f"Ratio Analysis for {company_name} attempt {attempt}/{retries}..."
            )
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
                message = re.sub(
                    r"\s+", " ", (m.group(1) if m else "Ratio Analysis failed")
                ).strip()
                err = RuntimeError(message)
                # Tally was busy or the report engine was not ready. Retry once.
                if "could not be setup" in message.lower() and attempt < retries:
                    last_err = err
                    logger.warning(
                        f"Ratio Analysis attempt {attempt}/{retries} for {company_name}: {message}"
                    )
                    time.sleep(5 * attempt)
                    continue
                raise err
            last_err = None
            break
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            last_err = e
            logger.warning(
                f"Ratio Analysis attempt {attempt}/{retries} for {company_name} "
                f"timed out/unreachable: {e}"
            )
            if attempt < retries:
                time.sleep(5 * attempt)
    if last_err is not None:
        raise RuntimeError(
            f"Ratio Analysis failed after {retries} attempt(s): {last_err}"
        ) from last_err

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
    """Write Ratio Analysis snapshot into local SQLite as pending (one row per FY)."""
    company_guid = (company_guid or "").strip() or company_name
    from_date = payload["from_date"]
    from_iso = from_date.isoformat() if hasattr(from_date, "isoformat") else str(from_date)[:10]
    to_date = payload["to_date"]
    to_iso = to_date.isoformat() if hasattr(to_date, "isoformat") else str(to_date)[:10]
    now = datetime.now(timezone.utc).isoformat()
    record = {
        "guid": f"{company_guid}#{from_iso}",
        "company_name": company_name,
        "company_guid": company_guid,
        "from_date": from_iso,
        "to_date": to_iso,
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
    books_from_iso: str = "",
) -> dict[str, Any]:
    """Fetch Ratio Analysis per FY (capped per run) → local DB → Supabase."""
    from .date_utils import iter_company_fy_windows
    from .financial_reports import _select_fy_windows_for_sync

    all_windows = list(
        iter_company_fy_windows(date.today(), books_from_iso, company_name)
    )
    windows = _select_fy_windows_for_sync(all_windows, company_name, force=False)
    staged_total = 0
    last_payload = None
    errors: list[str] = []
    for i, (fy_from, fy_to) in enumerate(windows):
        if i > 0:
            time.sleep(1.0)
        try:
            payload = fetch_ratio_analysis(
                company_name,
                from_date=fy_from,
                to_date=fy_to,
                books_from_iso=books_from_iso,
            )
            staged_total += stage_ratio_analysis_local(
                company_name, company_guid, payload
            )
            last_payload = payload
        except Exception as e:
            errors.append(f"{fy_from}→{fy_to}: {e}")
            logger.error(
                f"Ratio Analysis failed for {company_name} [{fy_from}→{fy_to}]: {e}"
            )

    if staged_total < 1:
        raise RuntimeError(
            "Failed to stage tally_ratio_analysis to local DB"
            + (f" ({'; '.join(errors)})" if errors else "")
        )

    pushed, push_err = push_ratio_analysis_to_supabase(company_name)
    payload = last_payload or {}
    days = payload.get("recv_turnover_days")
    logger.info(
        f"Ratio Analysis synced for {company_name}: years={len(windows)} "
        f"Recv. Turnover={days} days "
        f"last=({payload.get('from_date')} → {payload.get('to_date')}); "
        f"cloud={pushed}"
        + (f" push_err={push_err}" if push_err else "")
    )
    def _iso(v):
        return v.isoformat() if hasattr(v, "isoformat") else str(v)

    result = {
        "status": "ok" if not push_err and not errors else "partial",
        "years": len(windows),
        "recv_turnover_days": days,
        "from_date": _iso(payload.get("from_date")),
        "to_date": _iso(payload.get("to_date")),
        "staged_local": True,
        "pushed": pushed or 0,
    }
    if push_err:
        result["push_error"] = push_err
    if errors:
        result["errors"] = errors
    return result
