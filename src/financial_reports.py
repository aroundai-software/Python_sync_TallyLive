"""
Balance Sheet & Profit and Loss snapshots from Tally -> Supabase.

Heavy report exports run twice per local day (first sync + random 2–4 PM slot)
so normal hourly cycles stay fast and remote Tally is not hit every interval.

Usage:
    python -m src.financial_reports
    python -m src.financial_reports --company "Hospimed - 2026-27"
    python -m src.financial_reports --save
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Union
from xml.sax.saxutils import escape

import requests

# Fail-fast: prefer skip + next cycle over stalling Tally.
_REPORT_TIMEOUT: Union[int, tuple] = (20, 240)
_REPORT_RETRIES = 2
# Second daily slot: local time window [14:00, 16:00)
_AFTERNOON_START_HOUR = 14
_AFTERNOON_END_HOUR = 16

from .config import load_config
from .paths import get_app_dir
from .source_values import clean_source_xml, decode_response, source_number
from .tally_companies import get_all_companies_details, get_company_details

logger = logging.getLogger(__name__)

# Known Balance Sheet side classification (Tally reserved / common groups).
_BS_LIABILITY_GROUPS = {
    "capital account",
    "loans (liability)",
    "current liabilities",
    "suspense a/c",
    "branch / divisions",
    "profit & loss a/c",
    "profit and loss a/c",
}
_BS_ASSET_GROUPS = {
    "fixed assets",
    "current assets",
    "investments",
    "misc. expenses (asset)",
    "miscellaneous expenses (asset)",
}


@dataclass
class ReportLine:
    particulars: str
    amount: float
    sub_amount: Optional[float] = None
    section: Optional[str] = None  # liabilities/assets/expenses/incomes/unknown
    raw_main: Optional[str] = None
    raw_sub: Optional[str] = None


@dataclass
class ReportSnapshot:
    report_type: str  # balance_sheet | profit_and_loss
    company_name: str
    company_guid: str
    from_date: str  # YYYY-MM-DD
    to_date: str  # YYYY-MM-DD
    lines: list[ReportLine]
    raw_xml_path: Optional[str] = None


def _iso_to_tally(d: str) -> str:
    """YYYY-MM-DD -> YYYYMMDD"""
    return d.replace("-", "")


def _tally_to_iso(d: str) -> str:
    """YYYYMMDD -> YYYY-MM-DD"""
    d = d.strip()
    if len(d) == 8 and d.isdigit():
        return f"{d[0:4]}-{d[4:6]}-{d[6:8]}"
    return d


def _parse_amount(raw: Optional[str]) -> Optional[float]:
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        return float(source_number(text, 0.0))
    except Exception:
        try:
            return float(re.sub(r"[^\d.\-]", "", text) or 0)
        except Exception:
            return None


def indian_fy_start(as_on: Optional[date] = None, books_from_iso: Optional[str] = None) -> date:
    """Indian FY starts 1 April. Prefer books_from year when provided."""
    as_on = as_on or date.today()
    if books_from_iso:
        try:
            bf = date.fromisoformat(str(books_from_iso)[:10])
            if bf.month >= 4:
                return date(bf.year, 4, 1)
            return date(bf.year - 1, 4, 1)
        except Exception:
            pass
    if as_on.month >= 4:
        return date(as_on.year, 4, 1)
    return date(as_on.year - 1, 4, 1)


def _classify_bs_section(name: str) -> str:
    key = (name or "").strip().lower()
    key = key.replace("&amp;", "&")
    if key in _BS_LIABILITY_GROUPS or "liabilit" in key or key.startswith("capital"):
        return "liabilities"
    if key in _BS_ASSET_GROUPS or "asset" in key or "investment" in key:
        return "assets"
    if "profit" in key and "loss" in key:
        return "liabilities"
    if "difference in opening" in key:
        return "assets"
    return "unknown"


def build_report_xml(
    report_id: str,
    company_name: str,
    from_date_yyyymmdd: str,
    to_date_yyyymmdd: str,
) -> str:
    return f"""<ENVELOPE>
 <HEADER>
  <VERSION>1</VERSION>
  <TALLYREQUEST>Export</TALLYREQUEST>
  <TYPE>Data</TYPE>
  <ID>{escape(report_id)}</ID>
 </HEADER>
 <BODY>
  <DESC>
   <STATICVARIABLES>
    <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
    <SVCURRENTCOMPANY>{escape(company_name)}</SVCURRENTCOMPANY>
    <SVFROMDATE TYPE="Date">{from_date_yyyymmdd}</SVFROMDATE>
    <SVTODATE TYPE="Date">{to_date_yyyymmdd}</SVTODATE>
   </STATICVARIABLES>
  </DESC>
 </BODY>
</ENVELOPE>
"""


def export_tally_report(
    report_id: str,
    company_name: str,
    from_date: date,
    to_date: date,
    timeout: Union[int, tuple, None] = None,
    retries: int = _REPORT_RETRIES,
) -> str:
    """POST report export to Tally; return cleaned XML text. Retries on timeout."""
    config = load_config()
    xml_request = build_report_xml(
        report_id=report_id,
        company_name=company_name,
        from_date_yyyymmdd=from_date.strftime("%Y%m%d"),
        to_date_yyyymmdd=to_date.strftime("%Y%m%d"),
    )
    timeout = _REPORT_TIMEOUT if timeout is None else timeout
    last_err: Optional[Exception] = None
    for attempt in range(1, max(1, retries) + 1):
        try:
            response = requests.post(
                config["tally_url"],
                data=xml_request.encode("utf-8"),
                headers={"Content-Type": "application/xml", "Connection": "close"},
                timeout=timeout,
            )
            response.raise_for_status()
            text = clean_source_xml(decode_response(response))
            if "Unknown Request" in text:
                raise RuntimeError(
                    f"Tally rejected report id '{report_id}': {text.strip()}"
                )
            return text
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            last_err = e
            logging.warning(
                f"Tally report '{report_id}' attempt {attempt}/{retries} "
                f"timed out/unreachable: {e}"
            )
            if attempt < retries:
                time.sleep(2 * attempt)
        except Exception:
            raise
    raise RuntimeError(
        f"Tally report '{report_id}' failed after {retries} attempt(s): {last_err}"
    )


def parse_balance_sheet_xml(xml_text: str) -> list[ReportLine]:
    root = ET.fromstring(xml_text)
    names = [n.findtext("DSPACCNAME/DSPDISPNAME") or n.findtext("DSPDISPNAME") or "" for n in root.findall("BSNAME")]
    # BSNAME may wrap DSPACCNAME; also handle flat children order
    if not names:
        names = []
        for node in list(root):
            if node.tag == "BSNAME":
                names.append(
                    (node.findtext("DSPACCNAME/DSPDISPNAME") or node.findtext("DSPDISPNAME") or "").strip()
                )

    amts = root.findall("BSAMT")
    lines: list[ReportLine] = []
    for i, name_node in enumerate(root.findall("BSNAME")):
        particulars = (
            name_node.findtext("DSPACCNAME/DSPDISPNAME")
            or name_node.findtext("DSPDISPNAME")
            or ""
        ).strip()
        particulars = particulars.replace("&amp;", "&")
        amt_node = amts[i] if i < len(amts) else None
        raw_main = (amt_node.findtext("BSMAINAMT") if amt_node is not None else None) or ""
        raw_sub = (amt_node.findtext("BSSUBAMT") if amt_node is not None else None) or ""
        amount = _parse_amount(raw_main) or 0.0
        sub_amount = _parse_amount(raw_sub)
        lines.append(
            ReportLine(
                particulars=particulars,
                amount=amount,
                sub_amount=sub_amount,
                section=_classify_bs_section(particulars),
                raw_main=raw_main.strip() or None,
                raw_sub=raw_sub.strip() or None,
            )
        )
    return lines


def parse_profit_loss_xml(xml_text: str) -> list[ReportLine]:
    root = ET.fromstring(xml_text)
    # Condensed P&L: repeating DSPACCNAME + PLAMT siblings
    children = list(root)
    lines: list[ReportLine] = []
    i = 0
    while i < len(children):
        node = children[i]
        if node.tag == "DSPACCNAME":
            particulars = (node.findtext("DSPDISPNAME") or "").strip().replace("&amp;", "&")
            amt_node = children[i + 1] if i + 1 < len(children) and children[i + 1].tag == "PLAMT" else None
            raw_main = (amt_node.findtext("BSMAINAMT") if amt_node is not None else None) or ""
            raw_sub = (amt_node.findtext("PLSUBAMT") if amt_node is not None else None) or ""
            main = _parse_amount(raw_main)
            sub = _parse_amount(raw_sub)
            amount = main if main is not None else (sub if sub is not None else 0.0)
            section = "unknown"
            low = particulars.lower()
            if "cost of sales" in low or low.startswith("add:") or low.startswith("less:"):
                section = "expenses"
            elif any(k in low for k in ("purchase", "direct expenses", "indirect expenses", "gross profit c", "nett loss", "net loss", "closing stock")):
                section = "expenses"
            elif any(k in low for k in ("sales accounts", "direct incomes", "indirect incomes", "gross profit b", "nett profit", "net profit", "opening stock")):
                section = "incomes"
            lines.append(
                ReportLine(
                    particulars=particulars,
                    amount=amount,
                    sub_amount=sub,
                    section=section,
                    raw_main=raw_main.strip() or None,
                    raw_sub=raw_sub.strip() or None,
                )
            )
            i += 2 if amt_node is not None else 1
            continue
        i += 1
    return lines


def fetch_balance_sheet(
    company_name: str,
    company_guid: str,
    as_on: Optional[date] = None,
    books_from_iso: Optional[str] = None,
) -> ReportSnapshot:
    as_on = as_on or date.today()
    from_date = indian_fy_start(as_on, books_from_iso)
    xml_text = export_tally_report("Balance Sheet", company_name, from_date, as_on)
    lines = parse_balance_sheet_xml(xml_text)
    return ReportSnapshot(
        report_type="balance_sheet",
        company_name=company_name,
        company_guid=company_guid or "",
        from_date=from_date.isoformat(),
        to_date=as_on.isoformat(),
        lines=lines,
    )


def fetch_profit_and_loss(
    company_name: str,
    company_guid: str,
    as_on: Optional[date] = None,
    books_from_iso: Optional[str] = None,
) -> ReportSnapshot:
    as_on = as_on or date.today()
    from_date = indian_fy_start(as_on, books_from_iso)
    xml_text = export_tally_report("Profit and Loss", company_name, from_date, as_on)
    lines = parse_profit_loss_xml(xml_text)
    return ReportSnapshot(
        report_type="profit_and_loss",
        company_name=company_name,
        company_guid=company_guid or "",
        from_date=from_date.isoformat(),
        to_date=as_on.isoformat(),
        lines=lines,
    )


def _slug_key(particulars: str) -> str:
    key = re.sub(r"[^a-z0-9]+", "_", (particulars or "").strip().lower())
    return key.strip("_")[:80] or "line"


def _line_display_amount(line: ReportLine) -> float:
    """Absolute amount as shown in Tally UI (XML values are signed)."""
    return abs(float(line.amount or 0.0))


def _line_debit_credit(line: ReportLine) -> tuple[float, float, Optional[str]]:
    raw = float(line.amount or 0.0)
    # Prefer sub-amount when main is empty (common on P&L stock/purchase breakdown).
    if raw == 0.0 and line.sub_amount is not None:
        raw = float(line.sub_amount or 0.0)
    abs_amt = abs(raw)
    if abs_amt == 0:
        return 0.0, 0.0, None
    if raw < 0:
        return abs_amt, 0.0, "Dr"
    return 0.0, abs_amt, "Cr"


def _supabase_client():
    from supabase import create_client

    config = load_config()
    return create_client(config["supabase_url"], config["supabase_key"])


def _prefer_ledger_guid(candidates: list[dict], company_guid: str) -> Optional[dict]:
    """Prefer the guid scoped to this company when duplicates exist."""
    if not candidates:
        return None
    co = (company_guid or "").strip().lower()
    ranked = []
    for row in candidates:
        guid = (row.get("guid") or "").strip()
        if not guid:
            continue
        g = guid.lower()
        score = 0
        if co and g.startswith(co):
            score += 3
        if not g.startswith("00000000-0000-0000-0000-000000000000"):
            score += 1
        ranked.append((score, row))
    if not ranked:
        return None
    ranked.sort(key=lambda x: x[0], reverse=True)
    return ranked[0][1]


def _load_ledger_lookup(client, company_name: str, company_guid: str) -> dict[str, dict]:
    """
    Map lowercase ledger name -> {guid, ledger_type} from customers.
    Used to fill ledger_guid / mark row_type=ledger on report lines.
    """
    lookup: dict[str, list[dict]] = {}
    page_size = 1000
    offset = 0
    while True:
        res = (
            client.table("customers")
            .select("customer_name,guid,ledger_type")
            .eq("company_name", company_name)
            .range(offset, offset + page_size - 1)
            .execute()
        )
        rows = res.data or []
        if not rows:
            break
        for row in rows:
            name = (row.get("customer_name") or "").strip()
            if not name:
                continue
            lookup.setdefault(name.casefold(), []).append(row)
        if len(rows) < page_size:
            break
        offset += page_size

    resolved: dict[str, dict] = {}
    for key, candidates in lookup.items():
        best = _prefer_ledger_guid(candidates, company_guid)
        if best and best.get("guid"):
            resolved[key] = {
                "guid": best.get("guid"),
                "ledger_type": best.get("ledger_type"),
            }
    return resolved


def _is_report_label(particulars: str) -> bool:
    """True for calculated / label rows that are not real ledgers."""
    p = (particulars or "").strip().lower()
    if not p:
        return True
    if p.startswith("add:") or p.startswith("less:"):
        return True
    labels = (
        "cost of sales",
        "gross profit",
        "gross loss",
        "nett profit",
        "nett loss",
        "net profit",
        "net loss",
        "opening stock",
        "closing stock",
        "difference in opening balances",
    )
    return any(p.startswith(x) for x in labels)


def _build_line_row(
    *,
    report_id: str,
    snapshot: ReportSnapshot,
    line: ReportLine,
    idx: int,
    ledger_lookup: dict[str, dict],
) -> dict:
    debit, credit, dr_cr = _line_debit_credit(line)
    display = _line_display_amount(line)
    if display == 0.0 and line.sub_amount is not None:
        display = abs(float(line.sub_amount or 0.0))

    particulars = (line.particulars or "").strip()
    match = None if _is_report_label(particulars) else ledger_lookup.get(particulars.casefold())
    ledger_guid = (match or {}).get("guid")
    ledger_type = (match or {}).get("ledger_type")
    row_type = "ledger" if ledger_guid else "group"
    section = line.section
    if ledger_type:
        classified = _classify_bs_section(ledger_type)
        if classified != "unknown":
            section = classified
        elif section in (None, "", "unknown"):
            # P&L groups from parent type
            lt = ledger_type.lower()
            if any(x in lt for x in ("income", "sales")):
                section = "incomes"
            elif any(x in lt for x in ("expense", "purchase")):
                section = "expenses"

    return {
        "report_id": report_id,
        "company_name": snapshot.company_name,
        "company_guid": snapshot.company_guid,
        "row_type": row_type,
        "section": section,
        "particulars": particulars,
        "tally_group_name": ledger_type or particulars,
        "line_key": _slug_key(particulars),
        "parent_line_key": _slug_key(ledger_type) if ledger_type and row_type == "ledger" else None,
        "amount": round(display, 2),
        "debit": round(debit, 2),
        "credit": round(credit, 2),
        "dr_cr": dr_cr,
        "is_total": False,
        "is_calculated": _is_report_label(particulars),
        "ledger_guid": ledger_guid,
        "sort_order": idx,
    }


def _replace_report_lines(client, lines_table: str, report_id: str, rows: list[dict]) -> int:
    client.table(lines_table).delete().eq("report_id", report_id).execute()
    if not rows:
        return 0
    # PostgREST prefers modest batches
    batch_size = 200
    saved = 0
    for i in range(0, len(rows), batch_size):
        chunk = rows[i : i + batch_size]
        client.table(lines_table).insert(chunk).execute()
        saved += len(chunk)
    return saved


def save_balance_sheet_snapshot(snapshot: ReportSnapshot) -> dict:
    """Upsert BS report by company_guid and replace all lines (full snapshot)."""
    if not snapshot.company_guid:
        raise ValueError("company_guid is required to save balance sheet")
    client = _supabase_client()
    now_iso = datetime.now(timezone.utc).isoformat()

    # Cr (positive signed) → Liabilities; Dr (negative signed) → Assets (Tally sides).
    liab = 0.0
    assets = 0.0
    for line in snapshot.lines:
        raw = float(line.amount or 0.0)
        if raw == 0.0 and line.sub_amount is not None:
            raw = float(line.sub_amount or 0.0)
        if raw >= 0:
            liab += abs(raw)
        else:
            assets += abs(raw)
    gap = liab - assets
    if abs(gap) > 0.009:
        # Balancing figure Tally shows as "Difference in opening balances".
        if gap > 0:
            assets += abs(gap)
        else:
            liab += abs(gap)

    report_payload = {
        "company_name": snapshot.company_name,
        "company_guid": snapshot.company_guid,
        "as_on_date": snapshot.to_date,
        "format": "condensed",
        "total_liabilities": round(liab, 2),
        "total_assets": round(assets, 2),
        "synced_at": now_iso,
        "updated_at": now_iso,
    }
    upserted = (
        client.table("balance_sheet_reports")
        .upsert(report_payload, on_conflict="company_guid")
        .execute()
    )
    report_row = (upserted.data or [None])[0]
    if not report_row or not report_row.get("id"):
        # Some PostgREST configs return no representation — fetch id
        fetched = (
            client.table("balance_sheet_reports")
            .select("id")
            .eq("company_guid", snapshot.company_guid)
            .limit(1)
            .execute()
        )
        report_row = (fetched.data or [None])[0]
    if not report_row or not report_row.get("id"):
        raise RuntimeError("Failed to upsert balance_sheet_reports row")

    report_id = report_row["id"]
    ledger_lookup = _load_ledger_lookup(client, snapshot.company_name, snapshot.company_guid)
    line_rows = [
        _build_line_row(
            report_id=report_id,
            snapshot=snapshot,
            line=line,
            idx=idx,
            ledger_lookup=ledger_lookup,
        )
        for idx, line in enumerate(snapshot.lines)
    ]
    matched = sum(1 for r in line_rows if r.get("ledger_guid"))
    logging.info(
        f"Balance Sheet lines: {len(line_rows)} total, {matched} with ledger_guid "
        f"(lookup size={len(ledger_lookup)})"
    )

    saved_lines = _replace_report_lines(client, "balance_sheet_lines", report_id, line_rows)
    return {"report_id": report_id, "lines": saved_lines, "lines_with_guid": matched}


def _pl_line_abs(line: ReportLine) -> float:
    raw = float(line.amount or 0.0)
    if raw == 0.0 and line.sub_amount is not None:
        raw = float(line.sub_amount or 0.0)
    return abs(raw)


def _compute_pl_gross_nett(lines: list[ReportLine]) -> tuple[float, float]:
    """Gross / Nett from condensed Tally P&L (Sales − Cost of Sales ± Indirect)."""
    sales = 0.0
    direct_incomes = 0.0
    cost_of_sales = 0.0
    indirect_incomes = 0.0
    indirect_expenses = 0.0
    for line in lines:
        p = (line.particulars or "").strip().lower()
        amt = _pl_line_abs(line)
        if "sales accounts" in p:
            sales = amt
        elif "direct incomes" in p:
            direct_incomes = amt
        elif p.startswith("cost of sales"):
            cost_of_sales = amt
        elif "indirect incomes" in p:
            indirect_incomes = amt
        elif "indirect expenses" in p:
            indirect_expenses = amt
    gross = sales + direct_incomes - cost_of_sales
    nett = gross + indirect_incomes - indirect_expenses
    return gross, nett


def save_profit_loss_snapshot(snapshot: ReportSnapshot) -> dict:
    """Upsert P&L report by company_guid and replace all lines (full snapshot)."""
    if not snapshot.company_guid:
        raise ValueError("company_guid is required to save profit and loss")
    client = _supabase_client()
    now_iso = datetime.now(timezone.utc).isoformat()

    gross, nett = _compute_pl_gross_nett(snapshot.lines)
    report_payload = {
        "company_name": snapshot.company_name,
        "company_guid": snapshot.company_guid,
        "from_date": snapshot.from_date,
        "to_date": snapshot.to_date,
        "format": "condensed",
        "show_gross_profit": True,
        "gross_profit": round(gross, 2) if gross > 0 else 0,
        "gross_loss": round(abs(gross), 2) if gross < 0 else 0,
        "nett_profit": round(nett, 2) if nett > 0 else 0,
        "nett_loss": round(abs(nett), 2) if nett < 0 else 0,
        "synced_at": now_iso,
        "updated_at": now_iso,
    }
    upserted = (
        client.table("profit_loss_reports")
        .upsert(report_payload, on_conflict="company_guid")
        .execute()
    )
    report_row = (upserted.data or [None])[0]
    if not report_row or not report_row.get("id"):
        fetched = (
            client.table("profit_loss_reports")
            .select("id")
            .eq("company_guid", snapshot.company_guid)
            .limit(1)
            .execute()
        )
        report_row = (fetched.data or [None])[0]
    if not report_row or not report_row.get("id"):
        raise RuntimeError("Failed to upsert profit_loss_reports row")

    report_id = report_row["id"]
    ledger_lookup = _load_ledger_lookup(client, snapshot.company_name, snapshot.company_guid)
    line_rows = [
        _build_line_row(
            report_id=report_id,
            snapshot=snapshot,
            line=line,
            idx=idx,
            ledger_lookup=ledger_lookup,
        )
        for idx, line in enumerate(snapshot.lines)
    ]
    matched = sum(1 for r in line_rows if r.get("ledger_guid"))
    logging.info(
        f"Profit & Loss lines: {len(line_rows)} total, {matched} with ledger_guid "
        f"(lookup size={len(ledger_lookup)})"
    )

    saved_lines = _replace_report_lines(client, "profit_loss_lines", report_id, line_rows)
    return {"report_id": report_id, "lines": saved_lines, "lines_with_guid": matched}


def financial_reports_enabled() -> bool:
    return os.environ.get("FINANCIAL_REPORTS", "1").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


def _local_now() -> datetime:
    return datetime.now().astimezone()


def _parse_synced_at(value) -> Optional[datetime]:
    if not value:
        return None
    try:
        text = str(value).strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _schedule_path() -> str:
    data_dir = os.path.join(get_app_dir(), "data")
    os.makedirs(data_dir, exist_ok=True)
    return os.path.join(data_dir, "financial_reports_schedule.json")


def _afternoon_target_local(today: date) -> datetime:
    """
    Pick one random local time in [14:00, 16:00) for today and persist it
    so every sync cycle uses the same target.
    """
    path = _schedule_path()
    now = _local_now()
    tz = now.tzinfo
    try:
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if data.get("date") == today.isoformat() and data.get("afternoon_target"):
                target = datetime.fromisoformat(data["afternoon_target"])
                if target.tzinfo is None:
                    target = target.replace(tzinfo=tz)
                return target.astimezone(tz)
    except Exception as e:
        logging.warning(f"Could not read financial report schedule: {e}")

    window_secs = max(1, (_AFTERNOON_END_HOUR - _AFTERNOON_START_HOUR) * 3600)
    offset = random.randint(0, window_secs - 1)
    start = datetime(
        today.year, today.month, today.day, _AFTERNOON_START_HOUR, 0, 0, tzinfo=tz
    )
    target = start + timedelta(seconds=offset)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "date": today.isoformat(),
                    "afternoon_target": target.isoformat(),
                },
                f,
                indent=2,
            )
        logging.info(
            f"Financial reports afternoon slot for {today.isoformat()}: "
            f"{target.strftime('%H:%M:%S')} local"
        )
    except Exception as e:
        logging.warning(f"Could not save financial report schedule: {e}")
    return target


def _last_reports_synced_local(company_guid: str) -> Optional[datetime]:
    """Earliest of BS/P&L synced_at in local timezone; None if either missing."""
    guid = (company_guid or "").strip()
    if not guid:
        return None
    try:
        client = _supabase_client()
        bs = (
            client.table("balance_sheet_reports")
            .select("synced_at")
            .eq("company_guid", guid)
            .limit(1)
            .execute()
        )
        pl = (
            client.table("profit_loss_reports")
            .select("synced_at")
            .eq("company_guid", guid)
            .limit(1)
            .execute()
        )
        bs_at = _parse_synced_at(((bs.data or [None])[0] or {}).get("synced_at"))
        pl_at = _parse_synced_at(((pl.data or [None])[0] or {}).get("synced_at"))
        if not bs_at or not pl_at:
            return None
        earliest = min(bs_at, pl_at)
        return earliest.astimezone(_local_now().tzinfo)
    except Exception as e:
        logging.warning(f"Could not read last financial report sync time: {e}")
        return None


def financial_reports_due(company_guid: str) -> tuple[bool, str]:
    """
    Twice daily:
      1) first sync of the local calendar day
      2) once at a random time between 2:00 PM and 4:00 PM local
    """
    if os.environ.get("FULL_RECONCILE", "").strip().lower() in ("1", "true", "yes"):
        return True, "full_reconcile"

    now = _local_now()
    today = now.date()
    last = _last_reports_synced_local(company_guid)

    # Slot 1 — first sync of the day (no successful BS+P&L yet today)
    if last is None or last.date() < today:
        return True, "first_of_day"

    target = _afternoon_target_local(today)

    # Already completed the afternoon slot today
    if last >= target:
        return False, f"done_today(afternoon_target={target.strftime('%H:%M')})"

    # Morning done; wait until random afternoon target
    if now < target:
        return False, f"waiting_afternoon({target.strftime('%H:%M')})"

    # Now past target and last sync was before target → afternoon run
    return True, f"afternoon_slot({target.strftime('%H:%M')})"


def sync_financial_reports(
    company_name: str,
    company_guid: Optional[str] = None,
    books_from_iso: Optional[str] = None,
    as_on: Optional[date] = None,
    force: bool = False,
) -> dict:
    """
    Export BS + P&L from Tally and overwrite Supabase snapshots for this company.

    Schedule (unless force=True): first sync of the day, then once more at a
    random local time between 2 PM and 4 PM.
    """
    if not financial_reports_enabled() and not force:
        logging.info("Financial reports disabled (FINANCIAL_REPORTS=0); skipping.")
        return {
            "skipped": True,
            "reason": "disabled",
            "bs_line_count": 0,
            "pl_line_count": 0,
        }

    as_on = as_on or date.today()
    guid = (company_guid or "").strip()
    books_from = books_from_iso
    if not guid or not books_from:
        company = _resolve_company(company_name)
        guid = guid or (company.get("guid") or "")
        books_from = books_from or company.get("books_from") or company.get("financial_year_from")
        company_name = company.get("company_name") or company_name

    if not guid:
        raise ValueError(f"No company_guid available for {company_name}")

    if not force:
        due, reason = financial_reports_due(guid)
        if not due:
            logging.info(
                f"Financial reports for {company_name} not due ({reason}); skipping."
            )
            return {
                "skipped": True,
                "reason": reason,
                "company_name": company_name,
                "company_guid": guid,
                "bs_line_count": 0,
                "pl_line_count": 0,
            }
        logging.info(f"Financial reports due for {company_name}: {reason}")

    logging.info(f"Exporting Balance Sheet & Profit and Loss for {company_name}...")
    errors: list[str] = []
    bs = None
    pl = None
    try:
        bs = fetch_balance_sheet(
            company_name, guid, as_on=as_on, books_from_iso=books_from
        )
    except Exception as e:
        errors.append(f"Balance Sheet: {e}")
        logging.error(f"Balance Sheet export failed for {company_name}: {e}")

    # Brief pause so remote Tally can recover between heavy report exports
    time.sleep(1.0)

    try:
        pl = fetch_profit_and_loss(
            company_name, guid, as_on=as_on, books_from_iso=books_from
        )
    except Exception as e:
        errors.append(f"Profit and Loss: {e}")
        logging.error(f"Profit and Loss export failed for {company_name}: {e}")

    bs_saved = {"lines": 0}
    pl_saved = {"lines": 0}
    if bs is not None:
        bs_saved = save_balance_sheet_snapshot(bs)
    if pl is not None:
        pl_saved = save_profit_loss_snapshot(pl)

    logging.info(
        f"Saved financial reports for {company_name}: "
        f"BS lines={bs_saved.get('lines', 0)}, P&L lines={pl_saved.get('lines', 0)}"
    )

    if errors and bs is None and pl is None:
        raise RuntimeError("; ".join(errors))
    if errors:
        raise RuntimeError(
            "Partial financial reports sync: "
            + "; ".join(errors)
            + f" (saved BS={bs is not None}, P&L={pl is not None})"
        )

    return {
        "skipped": False,
        "company_name": company_name,
        "company_guid": guid,
        "as_on": as_on.isoformat(),
        "balance_sheet": bs_saved,
        "profit_and_loss": pl_saved,
        "bs_line_count": len(bs.lines) if bs else 0,
        "pl_line_count": len(pl.lines) if pl else 0,
    }


def _fmt_inr(value: float) -> str:
    sign = "-" if value < 0 else ""
    n = abs(value)
    s = f"{n:,.2f}"
    # rough Indian grouping for display in spike only
    parts = s.split(".")
    whole = parts[0].replace(",", "")
    if len(whole) <= 3:
        grouped = whole
    else:
        grouped = whole[-3:]
        rest = whole[:-3]
        while rest:
            grouped = rest[-2:] + "," + grouped
            rest = rest[:-2]
    return f"{sign}{grouped}.{parts[1]}"


def print_snapshot(snapshot: ReportSnapshot) -> None:
    print()
    print("=" * 72)
    print(f"{snapshot.report_type.upper()}  |  {snapshot.company_name}")
    print(f"company_guid: {snapshot.company_guid}")
    print(f"period: {snapshot.from_date}  ->  {snapshot.to_date}")
    if snapshot.raw_xml_path:
        print(f"raw xml: {snapshot.raw_xml_path}")
    print("-" * 72)
    print(f"{'Particulars':<40} {'Amount':>18} {'Section':<12}")
    print("-" * 72)
    for line in snapshot.lines:
        amt = _fmt_inr(line.amount)
        extra = f"  (sub {_fmt_inr(line.sub_amount)})" if line.sub_amount not in (None, 0) and line.sub_amount != line.amount else ""
        print(f"{line.particulars[:40]:<40} {amt:>18} {(line.section or ''):<12}{extra}")
    print("=" * 72)


def _resolve_company(company_name: Optional[str]) -> dict:
    if not company_name:
        return get_company_details()
    companies = get_all_companies_details()
    needle = company_name.strip().lower()
    for c in companies:
        if (c.get("company_name") or "").strip().lower() == needle:
            return c
    # partial match
    for c in companies:
        if needle in (c.get("company_name") or "").strip().lower():
            return c
    raise RuntimeError(f"Company not found in Tally: {company_name}")


def run_spike(
    company_name: Optional[str] = None,
    as_on: Optional[date] = None,
    out_dir: Optional[Path] = None,
) -> dict:
    company = _resolve_company(company_name)
    name = company.get("company_name") or ""
    guid = company.get("guid") or ""
    books_from = company.get("books_from") or company.get("financial_year_from")
    as_on = as_on or date.today()

    root = Path(__file__).resolve().parents[1]
    out_dir = out_dir or (root / "data" / "financial_report_spike")
    out_dir.mkdir(parents=True, exist_ok=True)

    bs_xml = export_tally_report(
        "Balance Sheet",
        name,
        indian_fy_start(as_on, books_from),
        as_on,
    )
    pl_xml = export_tally_report(
        "Profit and Loss",
        name,
        indian_fy_start(as_on, books_from),
        as_on,
    )

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe = re.sub(r"[^\w\-]+", "_", name).strip("_")[:60]
    bs_path = out_dir / f"{safe}_balance_sheet_{stamp}.xml"
    pl_path = out_dir / f"{safe}_profit_and_loss_{stamp}.xml"
    bs_path.write_text(bs_xml, encoding="utf-8")
    pl_path.write_text(pl_xml, encoding="utf-8")

    from_date = indian_fy_start(as_on, books_from)
    bs = ReportSnapshot(
        report_type="balance_sheet",
        company_name=name,
        company_guid=guid or "",
        from_date=from_date.isoformat(),
        to_date=as_on.isoformat(),
        lines=parse_balance_sheet_xml(bs_xml),
        raw_xml_path=str(bs_path),
    )
    pl = ReportSnapshot(
        report_type="profit_and_loss",
        company_name=name,
        company_guid=guid or "",
        from_date=from_date.isoformat(),
        to_date=as_on.isoformat(),
        lines=parse_profit_loss_xml(pl_xml),
        raw_xml_path=str(pl_path),
    )

    print_snapshot(bs)
    print_snapshot(pl)

    summary = {
        "company_name": name,
        "company_guid": guid,
        "as_on": as_on.isoformat(),
        "books_from": books_from,
        "balance_sheet": {
            "xml": str(bs_path),
            "lines": [asdict(x) for x in bs.lines],
        },
        "profit_and_loss": {
            "xml": str(pl_path),
            "lines": [asdict(x) for x in pl.lines],
        },
        "notes": [
            "Compare these Particulars/Amount rows with Tally Gateway -> Balance Sheet / Profit & Loss.",
            "Tally XML amounts are signed; UI may show absolute Dr/Cr — match absolute values + side.",
            "No Supabase write in this spike.",
        ],
    }
    summary_path = out_dir / f"{safe}_summary_{stamp}.json"
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(f"\nSummary JSON: {summary_path}")
    return summary


def main(argv: Optional[list[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description="Export Tally Balance Sheet & Profit and Loss")
    parser.add_argument("--company", help="Exact or partial Tally company name")
    parser.add_argument("--as-on", help="As-on / to-date YYYY-MM-DD (default: today)")
    parser.add_argument(
        "--save",
        action="store_true",
        help="Save snapshots to Supabase (requires sync write RLS policies)",
    )
    args = parser.parse_args(argv)

    as_on = date.fromisoformat(args.as_on) if args.as_on else date.today()
    try:
        if args.save:
            result = sync_financial_reports(company_name=args.company or "", as_on=as_on)
            print(json.dumps(result, indent=2))
            print("\nStep 3 OK: financial reports saved to Supabase.")
            return 0
        run_spike(company_name=args.company, as_on=as_on)
        print("\nExport + parse succeeded. Use --save to write to Supabase.")
        return 0
    except Exception as exc:
        logger.error("Financial reports FAILED: %s", exc)
        raise


if __name__ == "__main__":
    raise SystemExit(main())
