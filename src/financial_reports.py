"""
Balance Sheet & Profit and Loss snapshots from Tally -> local SQLite -> Supabase.

Heavy report exports run twice per local day (first sync + random 2–4 PM slot).
Remote Tally uses long waits; soft retries (~50 min, max 3/slot) recover from
timeouts without hourly storms. If Tally returns empty BS+P&L (reports not
available), the company is marked unavailable and skipped for ~7 days
(re-check later or on FULL_RECONCILE). Main sync runs FR after Phase 3 and
treats FR failure as a non-fatal warning.

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

# Second daily slot: local time window [14:00, 16:00)
_AFTERNOON_START_HOUR = 14
_AFTERNOON_END_HOUR = 16

# Soft retry within a slot (avoids hourly storms; still allows same-day recovery).
_MAX_ATTEMPTS_PER_SLOT = 3
_RETRY_DELAY_MINUTES = 50
# When Tally returns empty BS+P&L (reports not set up), skip that company for N days.
_UNAVAILABLE_RECHECK_DAYS = 7

# Local Tally (LAN/localhost): can afford longer report waits.
_LOCAL_REPORT_TIMEOUT: Union[int, tuple] = (15, 300)
_LOCAL_REPORT_RETRIES = 2
# Cloud / remote Tally (HostWinCloud etc.): reports often need several minutes.
# Prefer waiting and succeeding over a quick timeout that leaves BS/P&L empty.
# Override with FINANCIAL_REPORTS_REMOTE_TIMEOUT (seconds) / FINANCIAL_REPORTS_REMOTE_RETRIES.
_REMOTE_REPORT_TIMEOUT: Union[int, tuple] = (20, 360)
_REMOTE_REPORT_RETRIES = 2

from .config import load_config
from .paths import get_app_dir
from .source_values import clean_source_xml, decode_response, source_number
from .tally_companies import get_all_companies_details, get_company_details
from .tally_timeouts import is_remote_tally


def report_export_settings() -> tuple:
    """Return (timeout, retries, is_remote) tuned for local vs cloud Tally."""
    remote = is_remote_tally()
    if remote:
        try:
            read_secs = int(
                os.environ.get("FINANCIAL_REPORTS_REMOTE_TIMEOUT", "360").strip()
            )
        except ValueError:
            read_secs = 360
        read_secs = max(60, min(read_secs, 900))
        try:
            retries = int(
                os.environ.get("FINANCIAL_REPORTS_REMOTE_RETRIES", "2").strip()
            )
        except ValueError:
            retries = 2
        retries = max(1, min(retries, 4))
        return (20, read_secs), retries, True
    return _LOCAL_REPORT_TIMEOUT, _LOCAL_REPORT_RETRIES, False

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
    """
    Report period start = current Indian FY (1 April), not older books_from years.
    If books began after this FY start, use books_from (new company mid-year).
    """
    as_on = as_on or date.today()
    if as_on.month >= 4:
        fy = date(as_on.year, 4, 1)
    else:
        fy = date(as_on.year - 1, 4, 1)
    if books_from_iso:
        try:
            bf = date.fromisoformat(str(books_from_iso)[:10])
            if bf > fy:
                return bf
        except Exception:
            pass
    return fy


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
    retries: Optional[int] = None,
) -> str:
    """POST report export to Tally; return cleaned XML text. Retries on timeout."""
    config = load_config()
    auto_timeout, auto_retries, remote = report_export_settings()
    xml_request = build_report_xml(
        report_id=report_id,
        company_name=company_name,
        from_date_yyyymmdd=from_date.strftime("%Y%m%d"),
        to_date_yyyymmdd=to_date.strftime("%Y%m%d"),
    )
    timeout = auto_timeout if timeout is None else timeout
    retries = auto_retries if retries is None else retries
    last_err: Optional[Exception] = None
    for attempt in range(1, max(1, retries) + 1):
        try:
            logging.info(
                f"Tally report '{report_id}' "
                f"({'remote' if remote else 'local'}) attempt {attempt}/{retries}..."
            )
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
                # Remote Tally often needs a longer cool-down between heavy reports.
                pause = (8 * attempt) if remote else (2 * attempt)
                logging.info(
                    f"Waiting {pause}s before retrying Tally report '{report_id}'..."
                )
                time.sleep(pause)
        except Exception:
            raise
    raise RuntimeError(
        f"Tally report '{report_id}' failed after {retries} attempt(s): {last_err}"
    )


def _xml_local_tag(tag: Optional[str]) -> str:
    if not tag:
        return ""
    if "}" in tag:
        return tag.rsplit("}", 1)[-1]
    return tag


def _findall_by_tag(root: ET.Element, tag_name: str) -> list:
    """Find nodes by local tag name anywhere under root (flat or ENVELOPE-wrapped)."""
    direct = [n for n in list(root) if _xml_local_tag(n.tag) == tag_name]
    if direct:
        return direct
    return [n for n in root.iter() if _xml_local_tag(n.tag) == tag_name]


def _child_text(node: ET.Element, *paths: str) -> str:
    for path in paths:
        found = node.find(path)
        if found is not None and (found.text or "").strip():
            return (found.text or "").strip()
        # namespace-tolerant: walk children
        parts = path.split("/")
        cur = node
        ok = True
        for part in parts:
            nxt = None
            for ch in list(cur):
                if _xml_local_tag(ch.tag) == part:
                    nxt = ch
                    break
            if nxt is None:
                ok = False
                break
            cur = nxt
        if ok and (cur.text or "").strip():
            return (cur.text or "").strip()
    return ""


def parse_balance_sheet_xml(xml_text: str) -> list[ReportLine]:
    root = ET.fromstring(xml_text)
    name_nodes = _findall_by_tag(root, "BSNAME")
    amt_nodes = _findall_by_tag(root, "BSAMT")
    lines: list[ReportLine] = []
    for i, name_node in enumerate(name_nodes):
        particulars = (
            _child_text(name_node, "DSPACCNAME/DSPDISPNAME", "DSPDISPNAME")
            or ""
        ).replace("&amp;", "&")
        amt_node = amt_nodes[i] if i < len(amt_nodes) else None
        raw_main = _child_text(amt_node, "BSMAINAMT") if amt_node is not None else ""
        raw_sub = _child_text(amt_node, "BSSUBAMT") if amt_node is not None else ""
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
    # Prefer walking the subtree that actually holds DSPACCNAME rows
    scan_roots = [root]
    for cand in root.iter():
        if _xml_local_tag(cand.tag) in ("DATA", "COLLECTION", "ENVELOPE", "BODY"):
            scan_roots.append(cand)

    children = []
    for scan in scan_roots:
        kids = list(scan)
        if any(_xml_local_tag(k.tag) == "DSPACCNAME" for k in kids):
            children = kids
            break
    if not children:
        # Fallback: flatten alternating name/amount via iter order
        flat = [
            n
            for n in root.iter()
            if _xml_local_tag(n.tag) in ("DSPACCNAME", "PLAMT")
        ]
        children = flat

    lines: list[ReportLine] = []
    i = 0
    while i < len(children):
        node = children[i]
        if _xml_local_tag(node.tag) == "DSPACCNAME":
            particulars = (
                _child_text(node, "DSPDISPNAME") or ""
            ).replace("&amp;", "&")
            amt_node = None
            if i + 1 < len(children) and _xml_local_tag(children[i + 1].tag) == "PLAMT":
                amt_node = children[i + 1]
            raw_main = _child_text(amt_node, "BSMAINAMT") if amt_node is not None else ""
            raw_sub = _child_text(amt_node, "PLSUBAMT") if amt_node is not None else ""
            main = _parse_amount(raw_main)
            sub = _parse_amount(raw_sub)
            amount = main if main is not None else (sub if sub is not None else 0.0)
            section = "unknown"
            low = particulars.lower()
            if "cost of sales" in low or low.startswith("add:") or low.startswith("less:"):
                section = "expenses"
            elif any(
                k in low
                for k in (
                    "purchase",
                    "direct expenses",
                    "indirect expenses",
                    "gross profit c",
                    "nett loss",
                    "net loss",
                    "closing stock",
                )
            ):
                section = "expenses"
            elif any(
                k in low
                for k in (
                    "sales accounts",
                    "direct incomes",
                    "indirect incomes",
                    "gross profit b",
                    "nett profit",
                    "net profit",
                    "opening stock",
                )
            ):
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


def _load_ledger_lookup(company_name: str, company_guid: str) -> dict[str, dict]:
    """
    Map lowercase ledger name -> {guid, ledger_type} from local customers SQLite.
    Used to fill ledger_guid / mark row_type=ledger on report lines.
    """
    from .local_db import get_connection

    lookup: dict[str, list[dict]] = {}
    conn = get_connection()
    try:
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='customers'"
        )
        if not cur.fetchone():
            return {}
        rows = conn.execute(
            "SELECT customer_name, guid, ledger_type FROM customers "
            "WHERE company_name = ?",
            (company_name,),
        ).fetchall()
        for row in rows:
            name = (row["customer_name"] or "").strip()
            if not name:
                continue
            lookup.setdefault(name.casefold(), []).append(
                {
                    "customer_name": name,
                    "guid": row["guid"],
                    "ledger_type": row["ledger_type"],
                }
            )
    except Exception as e:
        logging.warning(f"Could not load local ledger lookup: {e}")
        return {}
    finally:
        conn.close()

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
    snapshot: ReportSnapshot,
    line: ReportLine,
    idx: int,
    ledger_lookup: dict[str, dict],
    line_prefix: str,
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

    line_key = _slug_key(particulars)
    # Local PK; stripped on Phase 3 upload (cloud lines use report_id).
    line_guid = f"{snapshot.company_guid}#{line_prefix}#{idx:05d}#{line_key}"
    return {
        "guid": line_guid,
        "company_name": snapshot.company_name,
        "company_guid": snapshot.company_guid,
        "row_type": row_type,
        "section": section,
        "particulars": particulars,
        "tally_group_name": ledger_type or particulars,
        "line_key": line_key,
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


def save_balance_sheet_snapshot(snapshot: ReportSnapshot) -> dict:
    """Stage BS report + lines into local SQLite (Phase 3 pushes to Supabase)."""
    if not snapshot.company_guid:
        raise ValueError("company_guid is required to save balance sheet")
    from .local_db import save_to_local_db

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
        "guid": snapshot.company_guid,
        "company_name": snapshot.company_name,
        "company_guid": snapshot.company_guid,
        "as_on_date": snapshot.to_date,
        "format": "condensed",
        "total_liabilities": round(liab, 2),
        "total_assets": round(assets, 2),
        "synced_at": now_iso,
        "updated_at": now_iso,
    }
    staged_reports = save_to_local_db(
        "balance_sheet_reports", [report_payload], snapshot.company_name
    )
    if staged_reports < 1:
        raise RuntimeError("Failed to stage balance_sheet_reports to local DB")

    ledger_lookup = _load_ledger_lookup(snapshot.company_name, snapshot.company_guid)
    line_rows = [
        _build_line_row(
            snapshot=snapshot,
            line=line,
            idx=idx,
            ledger_lookup=ledger_lookup,
            line_prefix="bs",
        )
        for idx, line in enumerate(snapshot.lines)
    ]
    matched = sum(1 for r in line_rows if r.get("ledger_guid"))
    logging.info(
        f"Balance Sheet lines: {len(line_rows)} total, {matched} with ledger_guid "
        f"(lookup size={len(ledger_lookup)})"
    )
    saved_lines = save_to_local_db(
        "balance_sheet_lines", line_rows, snapshot.company_name
    )
    return {
        "report_guid": snapshot.company_guid,
        "lines": saved_lines,
        "lines_with_guid": matched,
        "staged_local": True,
    }


def _pl_line_abs(line: ReportLine) -> float:
    raw = float(line.amount or 0.0)
    if raw == 0.0 and line.sub_amount is not None:
        raw = float(line.sub_amount or 0.0)
    return abs(raw)


def _pl_label_is(particulars: str, label: str) -> bool:
    """Exact / prefix match — avoids 'indirect incomes' matching 'direct incomes'."""
    p = (particulars or "").strip().lower()
    label = (label or "").strip().lower()
    if not label:
        return False
    return p == label or p.startswith(f"{label} ") or p.startswith(f"{label}(")


def _compute_pl_gross_nett(lines: list[ReportLine]) -> tuple[float, float]:
    """Gross / Nett from condensed Tally P&L (Sales + Direct Inc − Cost of Sales ± Indirect)."""
    sales = 0.0
    direct_incomes = 0.0
    cost_of_sales = 0.0
    indirect_incomes = 0.0
    indirect_expenses = 0.0
    for line in lines:
        p = (line.particulars or "").strip().lower()
        amt = _pl_line_abs(line)
        if _pl_label_is(p, "sales accounts"):
            sales = amt
        elif _pl_label_is(p, "direct incomes"):
            direct_incomes = amt
        elif p.startswith("cost of sales"):
            cost_of_sales = amt
        elif _pl_label_is(p, "indirect incomes"):
            indirect_incomes = amt
        elif _pl_label_is(p, "indirect expenses"):
            indirect_expenses = amt
    gross = sales + direct_incomes - cost_of_sales
    nett = gross + indirect_incomes - indirect_expenses
    return gross, nett


def save_profit_loss_snapshot(snapshot: ReportSnapshot) -> dict:
    """Stage P&L report + lines into local SQLite (Phase 3 pushes to Supabase)."""
    if not snapshot.company_guid:
        raise ValueError("company_guid is required to save profit and loss")
    from .local_db import save_to_local_db

    now_iso = datetime.now(timezone.utc).isoformat()

    gross, nett = _compute_pl_gross_nett(snapshot.lines)
    report_payload = {
        "guid": snapshot.company_guid,
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
    staged_reports = save_to_local_db(
        "profit_loss_reports", [report_payload], snapshot.company_name
    )
    if staged_reports < 1:
        raise RuntimeError("Failed to stage profit_loss_reports to local DB")

    ledger_lookup = _load_ledger_lookup(snapshot.company_name, snapshot.company_guid)
    line_rows = [
        _build_line_row(
            snapshot=snapshot,
            line=line,
            idx=idx,
            ledger_lookup=ledger_lookup,
            line_prefix="pl",
        )
        for idx, line in enumerate(snapshot.lines)
    ]
    matched = sum(1 for r in line_rows if r.get("ledger_guid"))
    logging.info(
        f"Profit & Loss lines: {len(line_rows)} total, {matched} with ledger_guid "
        f"(lookup size={len(ledger_lookup)})"
    )
    saved_lines = save_to_local_db(
        "profit_loss_lines", line_rows, snapshot.company_name
    )
    return {
        "report_guid": snapshot.company_guid,
        "lines": saved_lines,
        "lines_with_guid": matched,
        "staged_local": True,
    }


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


def _attempts_path() -> str:
    data_dir = os.path.join(get_app_dir(), "data")
    os.makedirs(data_dir, exist_ok=True)
    return os.path.join(data_dir, "financial_reports_attempts.json")


def _unavailable_path() -> str:
    data_dir = os.path.join(get_app_dir(), "data")
    os.makedirs(data_dir, exist_ok=True)
    return os.path.join(data_dir, "financial_reports_unavailable.json")


def _unavailable_recheck_days() -> int:
    try:
        days = int(
            os.environ.get(
                "FINANCIAL_REPORTS_UNAVAILABLE_DAYS",
                str(_UNAVAILABLE_RECHECK_DAYS),
            ).strip()
        )
    except ValueError:
        days = _UNAVAILABLE_RECHECK_DAYS
    return max(1, min(days, 90))


def _load_unavailable() -> dict:
    path = _unavailable_path()
    try:
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception as e:
        logging.warning(f"Could not read financial reports unavailable map: {e}")
    return {}


def _save_unavailable(data: dict) -> None:
    try:
        with open(_unavailable_path(), "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        logging.warning(f"Could not save financial reports unavailable map: {e}")


def financial_reports_unavailable_until(
    company_guid: str,
) -> Optional[date]:
    """
    If this company was marked as having no usable BS/P&L in Tally, return the
    local date until which we should skip (inclusive). None = not unavailable.
    """
    guid = (company_guid or "").strip()
    if not guid:
        return None
    entry = _load_unavailable().get(guid) or {}
    until_raw = entry.get("until")
    if not until_raw:
        return None
    try:
        until = date.fromisoformat(str(until_raw)[:10])
    except Exception:
        return None
    if _local_now().date() <= until:
        return until
    return None


def mark_financial_reports_unavailable(
    company_guid: str,
    reason: str = "empty_reports",
) -> date:
    """Skip FR for this company until today + recheck days (default 7)."""
    guid = (company_guid or "").strip()
    days = _unavailable_recheck_days()
    until = _local_now().date() + timedelta(days=days)
    if not guid:
        return until
    data = _load_unavailable()
    data[guid] = {
        "until": until.isoformat(),
        "reason": reason,
        "marked_at": _local_now().isoformat(),
    }
    _save_unavailable(data)
    logging.warning(
        f"Financial reports marked unavailable for {guid} until {until.isoformat()} "
        f"({reason}); will re-check after that date or on FULL_RECONCILE=1."
    )
    return until


def clear_financial_reports_unavailable(company_guid: str) -> None:
    guid = (company_guid or "").strip()
    if not guid:
        return
    data = _load_unavailable()
    if guid in data:
        data.pop(guid, None)
        _save_unavailable(data)
        logging.info(f"Cleared financial-reports unavailable flag for {guid}")


def _errors_look_like_empty_reports(errors: list[str]) -> bool:
    """True when failure is empty parse only (not timeout / connection)."""
    if not errors:
        return False
    joined = " ".join(errors).lower()
    if any(
        x in joined
        for x in (
            "timed out",
            "timeout",
            "connection",
            "max retries",
            "refused",
            "unreachable",
        )
    ):
        return False
    empty_markers = sum(1 for e in errors if "no parseable lines" in e.lower())
    return empty_markers >= 1


def _load_attempts() -> dict:
    path = _attempts_path()
    try:
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception as e:
        logging.warning(f"Could not read financial report attempts: {e}")
    return {}


def _save_attempts(data: dict) -> None:
    try:
        with open(_attempts_path(), "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        logging.warning(f"Could not save financial report attempts: {e}")


def _slot_from_reason(reason: str) -> str:
    r = (reason or "").lower()
    if "afternoon" in r:
        return "afternoon"
    return "morning"


def _fresh_attempts_entry(today: str) -> dict:
    return {
        "date": today,
        "morning_attempts": 0,
        "afternoon_attempts": 0,
        "morning_exhausted": False,
        "afternoon_exhausted": False,
        "next_retry_after": None,
        "last_error_at": None,
        "last_reason": None,
    }


def _normalize_attempts_entry(entry: dict, today: str) -> dict:
    """Migrate legacy morning_failed/afternoon_failed flags to soft-retry fields."""
    if not entry or entry.get("date") != today:
        return _fresh_attempts_entry(today)
    out = _fresh_attempts_entry(today)
    out["morning_attempts"] = int(entry.get("morning_attempts") or 0)
    out["afternoon_attempts"] = int(entry.get("afternoon_attempts") or 0)
    out["morning_exhausted"] = bool(entry.get("morning_exhausted"))
    out["afternoon_exhausted"] = bool(entry.get("afternoon_exhausted"))
    # Legacy hard-fail → treat as exhausted for that slot
    if entry.get("morning_failed"):
        out["morning_exhausted"] = True
        out["morning_attempts"] = max(out["morning_attempts"], _MAX_ATTEMPTS_PER_SLOT)
    if entry.get("afternoon_failed"):
        out["afternoon_exhausted"] = True
        out["afternoon_attempts"] = max(
            out["afternoon_attempts"], _MAX_ATTEMPTS_PER_SLOT
        )
    out["next_retry_after"] = entry.get("next_retry_after")
    out["last_error_at"] = entry.get("last_error_at")
    out["last_reason"] = entry.get("last_reason")
    return out


def _parse_retry_after(value) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_local_now().tzinfo)
        return dt.astimezone(_local_now().tzinfo)
    except Exception:
        return None


def mark_financial_reports_attempt_failed(company_guid: str, reason: str) -> dict:
    """
    Soft failure: increment slot attempts, set next_retry_after (~50 min).
    Exhaust the slot only after _MAX_ATTEMPTS_PER_SLOT failures.
    Returns the updated attempts entry.
    """
    guid = (company_guid or "").strip()
    today = _local_now().date().isoformat()
    data = _load_attempts()
    entry = _normalize_attempts_entry(data.get(guid) or {}, today)
    slot = _slot_from_reason(reason)
    key_attempts = f"{slot}_attempts"
    key_exhausted = f"{slot}_exhausted"
    entry[key_attempts] = int(entry.get(key_attempts) or 0) + 1
    entry["last_error_at"] = _local_now().isoformat()
    entry["last_reason"] = reason
    if entry[key_attempts] >= _MAX_ATTEMPTS_PER_SLOT:
        entry[key_exhausted] = True
        entry["next_retry_after"] = None
        logging.warning(
            f"Financial reports {slot} slot exhausted for {guid} "
            f"after {entry[key_attempts]} attempt(s); wait for next scheduled slot."
        )
    else:
        retry_at = _local_now() + timedelta(minutes=_RETRY_DELAY_MINUTES)
        entry["next_retry_after"] = retry_at.isoformat()
        entry[key_exhausted] = False
        logging.warning(
            f"Financial reports {slot} attempt {entry[key_attempts]}/"
            f"{_MAX_ATTEMPTS_PER_SLOT} failed for {guid}; "
            f"next retry after {retry_at.strftime('%H:%M')} local."
        )
    data[guid] = entry
    _save_attempts(data)
    return entry


def mark_financial_reports_slot_failed(company_guid: str, reason: str) -> None:
    """Backward-compatible alias → soft attempt failure."""
    mark_financial_reports_attempt_failed(company_guid, reason)


def clear_financial_reports_retry_state(company_guid: str) -> None:
    """Clear retry delay after a successful BS+P&L stage."""
    guid = (company_guid or "").strip()
    if not guid:
        return
    today = _local_now().date().isoformat()
    data = _load_attempts()
    entry = _normalize_attempts_entry(data.get(guid) or {}, today)
    entry["next_retry_after"] = None
    entry["last_error_at"] = None
    entry["last_reason"] = None
    data[guid] = entry
    _save_attempts(data)


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
    """
    Earliest of BS/P&L synced_at from local SQLite when both headers exist
    and each has at least one staged line — used for 2×/day schedule.
    """
    guid = (company_guid or "").strip()
    if not guid:
        return None
    try:
        from .local_db import get_connection

        conn = get_connection()
        try:
            times = []
            pairs = (
                ("balance_sheet_reports", "balance_sheet_lines"),
                ("profit_loss_reports", "profit_loss_lines"),
            )
            for reports_table, lines_table in pairs:
                cur = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                    (reports_table,),
                )
                if not cur.fetchone():
                    return None
                row = conn.execute(
                    f'SELECT synced_at, company_name FROM "{reports_table}" '
                    f'WHERE company_guid = ? OR guid = ? LIMIT 1',
                    (guid, guid),
                ).fetchone()
                if not row:
                    return None
                parsed = _parse_synced_at(row["synced_at"])
                if not parsed:
                    return None
                # Require lines so an empty/header-only stage does not close the slot
                cur_lines = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                    (lines_table,),
                )
                if not cur_lines.fetchone():
                    return None
                company_name = (row["company_name"] or "").strip()
                if company_name:
                    line_count = conn.execute(
                        f'SELECT COUNT(*) AS c FROM "{lines_table}" '
                        f"WHERE company_name = ?",
                        (company_name,),
                    ).fetchone()["c"]
                else:
                    line_count = conn.execute(
                        f'SELECT COUNT(*) AS c FROM "{lines_table}" '
                        f"WHERE company_guid = ?",
                        (guid,),
                    ).fetchone()["c"]
                if not line_count:
                    return None
                times.append(parsed)
            return min(times).astimezone(_local_now().tzinfo)
        finally:
            conn.close()
    except Exception as e:
        logging.warning(f"Could not read last financial report sync time: {e}")
        return None


def financial_reports_due(company_guid: str) -> tuple[bool, str]:
    """
    Twice daily:
      1) first sync of the local calendar day
      2) once at a random time between 2:00 PM and 4:00 PM local

    Soft retries: after a timeout, wait ~50 minutes (next_retry_after) and
    retry up to 3 times per slot before moving on.
    """
    if os.environ.get("FULL_RECONCILE", "").strip().lower() in ("1", "true", "yes"):
        return True, "full_reconcile"

    now = _local_now()
    today = now.date()
    last = _last_reports_synced_local(company_guid)
    attempts = _normalize_attempts_entry(
        _load_attempts().get((company_guid or "").strip()) or {},
        today.isoformat(),
    )
    morning_exhausted = bool(attempts.get("morning_exhausted"))
    afternoon_exhausted = bool(attempts.get("afternoon_exhausted"))
    retry_after = _parse_retry_after(attempts.get("next_retry_after"))
    target = _afternoon_target_local(today)

    def _respect_retry_delay(slot_label: str) -> tuple[bool, str]:
        if retry_after and now < retry_after:
            return False, (
                f"deferred_retry_after({retry_after.strftime('%H:%M')},{slot_label})"
            )
        return True, slot_label

    # Slot 1 — first sync of the day (no complete local BS+P&L yet today)
    if last is None or last.date() < today:
        if morning_exhausted:
            if afternoon_exhausted:
                return False, "both_slots_exhausted_today"
            if now < target:
                return False, (
                    f"morning_exhausted_wait_afternoon({target.strftime('%H:%M')})"
                )
            return _respect_retry_delay(f"afternoon_slot({target.strftime('%H:%M')})")
        attempts_n = int(attempts.get("morning_attempts") or 0)
        if attempts_n > 0:
            return _respect_retry_delay(f"morning_retry({attempts_n + 1})")
        return True, "first_of_day"

    # Already completed the afternoon slot today
    if last >= target:
        return False, f"done_today(afternoon_target={target.strftime('%H:%M')})"

    # Morning done; wait until random afternoon target
    if now < target:
        return False, f"waiting_afternoon({target.strftime('%H:%M')})"

    if afternoon_exhausted:
        return False, "afternoon_exhausted_today"

    attempts_n = int(attempts.get("afternoon_attempts") or 0)
    if attempts_n > 0:
        return _respect_retry_delay(
            f"afternoon_retry({attempts_n + 1},{target.strftime('%H:%M')})"
        )
    return True, f"afternoon_slot({target.strftime('%H:%M')})"


def sync_financial_reports(
    company_name: str,
    company_guid: Optional[str] = None,
    books_from_iso: Optional[str] = None,
    as_on: Optional[date] = None,
    force: bool = False,
    push_now: bool = False,
) -> dict:
    """
    Export BS + P&L from Tally and stage snapshots into local SQLite.

    Returns a dict with status:
      ok | skipped | deferred | failed
    Does not raise on Tally timeout (soft retry); callers treat failed/deferred
    as non-fatal for the overall sync cycle.
    """
    if not financial_reports_enabled() and not force:
        logging.info("Financial reports disabled (FINANCIAL_REPORTS=0); skipping.")
        return {
            "status": "skipped",
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
        return {
            "status": "failed",
            "skipped": False,
            "reason": f"No company_guid available for {company_name}",
            "bs_line_count": 0,
            "pl_line_count": 0,
        }

    full_reconcile = os.environ.get("FULL_RECONCILE", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    # Previously empty BS+P&L → skip until re-check date (unless force / full reconcile).
    if not force and not full_reconcile:
        until = financial_reports_unavailable_until(guid)
        if until is not None:
            reason = f"not_available_until({until.isoformat()})"
            logging.info(
                f"Financial reports for {company_name} skipped — Tally reports "
                f"marked unavailable until {until.isoformat()}."
            )
            return {
                "status": "skipped",
                "skipped": True,
                "reason": reason,
                "company_name": company_name,
                "company_guid": guid,
                "bs_line_count": 0,
                "pl_line_count": 0,
            }
    elif full_reconcile:
        clear_financial_reports_unavailable(guid)

    due_reason = "forced"
    if not force:
        due, due_reason = financial_reports_due(guid)
        if not due:
            status = "deferred" if str(due_reason).startswith("deferred_") else "skipped"
            logging.info(
                f"Financial reports for {company_name} not due ({due_reason}); skipping."
            )
            return {
                "status": status,
                "skipped": True,
                "deferred": status == "deferred",
                "reason": due_reason,
                "company_name": company_name,
                "company_guid": guid,
                "bs_line_count": 0,
                "pl_line_count": 0,
            }
        logging.info(f"Financial reports due for {company_name}: {due_reason}")

    _, _, remote = report_export_settings()
    logging.info(
        f"Exporting Balance Sheet & Profit and Loss for {company_name} "
        f"({'remote/cloud Tally' if remote else 'local Tally'})..."
    )
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

    # If BS already timed out on remote Tally, skip P&L this attempt.
    # Local Tally still tries both — LAN can usually finish.
    if bs is None and remote:
        errors.append(
            "Profit and Loss: skipped after Balance Sheet failure (remote Tally)"
        )
        logging.warning(
            f"Skipping Profit and Loss for {company_name} after remote BS failure "
            f"to avoid further Tally load this attempt."
        )
    else:
        time.sleep(5.0 if remote else 0.5)
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
        if not bs.lines:
            errors.append("Balance Sheet: Tally returned no parseable lines")
            logging.error(f"Balance Sheet empty for {company_name}")
            bs = None
        else:
            bs_saved = save_balance_sheet_snapshot(bs)
    if pl is not None:
        if not pl.lines:
            errors.append("Profit and Loss: Tally returned no parseable lines")
            logging.error(f"Profit and Loss empty for {company_name}")
            pl = None
        else:
            pl_saved = save_profit_loss_snapshot(pl)

    logging.info(
        f"Staged financial reports locally for {company_name}: "
        f"BS lines={bs_saved.get('lines', 0)}, P&L lines={pl_saved.get('lines', 0)}"
    )

    if errors and (bs is None or pl is None):
        # Both empty (no usable lines) → reports not available in Tally; skip ~7 days.
        if bs is None and pl is None and _errors_look_like_empty_reports(errors):
            until = mark_financial_reports_unavailable(guid, reason="empty_reports")
            msg = (
                f"Tally BS/P&L not available (empty reports); "
                f"skipping until {until.isoformat()}"
            )
            logging.warning(f"{msg} for {company_name}")
            return {
                "status": "skipped",
                "skipped": True,
                "reason": f"not_available_until({until.isoformat()})",
                "company_name": company_name,
                "company_guid": guid,
                "as_on": as_on.isoformat(),
                "balance_sheet": bs_saved,
                "profit_and_loss": pl_saved,
                "bs_line_count": 0,
                "pl_line_count": 0,
                "errors": errors,
            }

        attempt_entry = mark_financial_reports_attempt_failed(guid, due_reason)
        retry_after = attempt_entry.get("next_retry_after")
        msg = "; ".join(errors)
        if bs is not None and pl is None:
            msg = (
                f"Partial financial reports: {msg} "
                f"(saved BS=True, P&L=False)"
            )
        elif bs is None and pl is not None:
            msg = (
                f"Partial financial reports: {msg} "
                f"(saved BS=False, P&L=True)"
            )
        return {
            "status": "failed",
            "skipped": False,
            "deferred": bool(retry_after),
            "reason": msg,
            "company_name": company_name,
            "company_guid": guid,
            "as_on": as_on.isoformat(),
            "balance_sheet": bs_saved,
            "profit_and_loss": pl_saved,
            "bs_line_count": len(bs.lines) if bs else 0,
            "pl_line_count": len(pl.lines) if pl else 0,
            "next_retry_after": retry_after,
            "errors": errors,
        }

    clear_financial_reports_retry_state(guid)
    clear_financial_reports_unavailable(guid)

    push_results = None
    push_errors: list[str] = []
    if push_now:
        from .local_db import push_financial_reports_for_company

        client = _supabase_client()
        push_results, push_errors = push_financial_reports_for_company(
            client, company_name
        )

    return {
        "status": "ok",
        "skipped": False,
        "company_name": company_name,
        "company_guid": guid,
        "as_on": as_on.isoformat(),
        "balance_sheet": bs_saved,
        "profit_and_loss": pl_saved,
        "bs_line_count": len(bs.lines) if bs else 0,
        "pl_line_count": len(pl.lines) if pl else 0,
        "push": push_results,
        "push_errors": push_errors,
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
        help="Stage to local DB and push to Supabase (requires sync write RLS policies)",
    )
    args = parser.parse_args(argv)

    as_on = date.fromisoformat(args.as_on) if args.as_on else date.today()
    try:
        if args.save:
            result = sync_financial_reports(
                company_name=args.company or "",
                as_on=as_on,
                force=True,
                push_now=True,
            )
            print(json.dumps(result, indent=2, default=str))
            if result.get("status") == "ok":
                if result.get("push_errors"):
                    print("\nStaged OK but cloud push had errors:", result["push_errors"])
                    return 1
                print("\nOK: financial reports staged locally and pushed to Supabase.")
                return 0
            print(f"\nFinancial reports status={result.get('status')}: {result.get('reason')}")
            return 1
        run_spike(company_name=args.company, as_on=as_on)
        print("\nExport + parse succeeded. Use --save to stage locally and push to Supabase.")
        return 0
    except Exception as exc:
        logger.error("Financial reports FAILED: %s", exc)
        raise


if __name__ == "__main__":
    raise SystemExit(main())
