"""
Cash Flow Summary (month drill-down) reconstructed from local daybook + ledger types.

Tally's native "Cash Flow Summary" report is not available via Export Data API.
We approximate it by classifying cash/bank voucher lines and rolling each ledger
up to Tally's reserved primary group (Current Assets, Direct Expenses, …) using
the live Group hierarchy — matching Tally's Summary presentation.
"""

from __future__ import annotations

import calendar
import html
import logging
import re
import sqlite3
from collections import defaultdict
from datetime import date
from typing import Any, Optional
from xml.sax.saxutils import escape

import requests

from .config import load_config
from .local_db import get_db_path
from .source_values import clean_source_xml, decode_response
from .tally_timeouts import tally_http_timeout

logger = logging.getLogger(__name__)

# Reserved / known child groups → Cash Flow Summary primary category (as Tally shows).
_TO_PRIMARY = {
    "cash-in-hand": "Current Assets",
    "bank accounts": "Current Assets",
    "bank od a/c": "Current Assets",
    "bank od accounts": "Current Assets",
    "stock-in-hand": "Current Assets",
    "sundry debtors": "Current Assets",
    "deposits (asset)": "Current Assets",
    "loans & advances (asset)": "Current Assets",
    "current assets": "Current Assets",
    "sundry creditors": "Current Liabilities",
    "duties & taxes": "Current Liabilities",
    "duties and taxes": "Current Liabilities",
    "provisions": "Current Liabilities",
    "current liabilities": "Current Liabilities",
    "indirect expenses": "Indirect Expenses",
    "indirect incomes": "Indirect Incomes",
    "direct expenses": "Direct Expenses",
    "direct incomes": "Direct Incomes",
    "sales accounts": "Sales Accounts",
    "purchase accounts": "Purchase Accounts",
    "fixed assets": "Fixed Assets",
    "capital account": "Capital Account",
    "loans (liability)": "Loans (Liability)",
    "secured loans": "Loans (Liability)",
    "unsecured loans": "Loans (Liability)",
    "investments": "Investments",
    "suspense a/c": "Suspense A/c",
    "branch / divisions": "Branch / Divisions",
    "misc. expenses (asset)": "Misc. Expenses (Asset)",
    "profit & loss a/c": "Profit & Loss A/c",
    "profit and loss a/c": "Profit & Loss A/c",
}

# Canonical primary names (also used as walk stop-points).
_PRIMARY_DISPLAY = {v.lower(): v for v in _TO_PRIMARY.values()}
_PRIMARY_DISPLAY.update({k: v for k, v in _TO_PRIMARY.items() if k == v.lower()})

_CASH_TYPES = {
    "cash-in-hand",
    "bank accounts",
    "bank od a/c",
    "bank od accounts",
}

_MONTH_NAMES = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}


def _is_debit(value: Any) -> bool:
    return str(value).strip().lower() in ("1", "true", "yes")


def _load_ledger_types(conn: sqlite3.Connection, company_name: str) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        rows = conn.execute(
            "SELECT customer_name, ledger_type FROM customers WHERE company_name = ?",
            (company_name,),
        ).fetchall()
    except Exception:
        return out
    for name, ledger_type in rows:
        key = (name or "").strip().lower()
        if key:
            out[key] = (ledger_type or "").strip()
    return out


def fetch_group_parents(company_name: str) -> dict[str, str]:
    """
    group_name (original case) -> parent group name.
    Also returns a lowercase index via resolve helper.
    """
    config = load_config()
    xml = f"""
<ENVELOPE>
 <HEADER><VERSION>1</VERSION><TALLYREQUEST>Export</TALLYREQUEST>
 <TYPE>Collection</TYPE><ID>CFGroups</ID></HEADER>
 <BODY><DESC>
  <STATICVARIABLES>
   <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
   <SVCURRENTCOMPANY>{escape(company_name)}</SVCURRENTCOMPANY>
  </STATICVARIABLES>
  <TDL><TDLMESSAGE>
   <COLLECTION NAME="CFGroups">
    <TYPE>Group</TYPE>
    <FETCH>NAME, PARENT</FETCH>
   </COLLECTION>
  </TDLMESSAGE></TDL>
 </DESC></BODY>
</ENVELOPE>"""
    try:
        response = requests.post(
            config["tally_url"],
            data=xml.encode("utf-8"),
            headers={"Content-Type": "application/xml", "Connection": "close"},
            timeout=tally_http_timeout(local_read=60, remote_read=120),
        )
        response.raise_for_status()
        text = clean_source_xml(decode_response(response))
    except Exception as e:
        logger.warning(f"Could not fetch group hierarchy for Cash Flow Summary: {e}")
        return {}

    parents: dict[str, str] = {}
    blocks = re.findall(
        r'<GROUP[^>]*\bNAME="([^"]+)"[^>]*>(.*?)</GROUP>',
        text,
        re.I | re.S,
    )
    for raw_name, body in blocks:
        name = html.unescape(raw_name).strip()
        m = re.search(r"<PARENT[^>]*>([^<]*)</PARENT>", body, re.I)
        parent = html.unescape(m.group(1)).strip() if m else ""
        if name:
            parents[name] = parent
    if not parents:
        # Fallback looser parse
        for m in re.finditer(
            r'<GROUP NAME="([^"]+)"[\s\S]*?<PARENT[^>]*>([^<]*)</PARENT>',
            text,
            re.I,
        ):
            parents[html.unescape(m.group(1)).strip()] = html.unescape(
                m.group(2)
            ).strip()
    logger.info(
        f"Cash Flow Summary: loaded {len(parents)} groups for {company_name}"
    )
    return parents


def _parent_lookup(group_parents: dict[str, str]) -> dict[str, str]:
    """Lowercase name -> parent (original case preserved on values)."""
    return {k.lower(): v for k, v in group_parents.items()}


def resolve_primary_group(
    start_group: str,
    group_parents: dict[str, str],
) -> str:
    """
    Walk parent chain until a Tally reserved / Cash Flow primary group is found.
    """
    if not start_group:
        return "Others"
    by_lower = _parent_lookup(group_parents)
    cur = start_group.strip()
    seen: set[str] = set()
    for _ in range(24):
        if not cur:
            break
        key = cur.lower()
        if key in ("primary", "null"):
            break
        if key in _TO_PRIMARY:
            return _TO_PRIMARY[key]
        if key in _PRIMARY_DISPLAY:
            return _PRIMARY_DISPLAY[key]
        if key in seen:
            break
        seen.add(key)
        nxt = by_lower.get(key)
        if not nxt:
            break
        cur = nxt.strip()
    # Unknown custom top — keep original label rather than inventing.
    return start_group.strip() or "Others"


def _is_cash(name: str, ledger_types: dict[str, str]) -> bool:
    if not name:
        return False
    return ledger_types.get(name.lower(), "").lower() in _CASH_TYPES


def _category(
    name: str,
    ledger_types: dict[str, str],
    group_parents: dict[str, str],
) -> str:
    if not name:
        return "Others"
    immediate = ledger_types.get(name.lower(), "").strip()
    if not immediate:
        return "Others"
    return resolve_primary_group(immediate, group_parents)


def month_date_range(
    period_label: str,
    from_date: date,
    to_date: date,
) -> Optional[tuple[date, date]]:
    """Resolve a Tally month label (e.g. April) within [from_date, to_date]."""
    month_num = _MONTH_NAMES.get((period_label or "").strip().lower())
    if not month_num:
        return None
    candidates = {from_date.year, to_date.year}
    for y in range(from_date.year, to_date.year + 1):
        candidates.add(y)
    best: Optional[tuple[date, date]] = None
    for year in sorted(candidates):
        last_day = calendar.monthrange(year, month_num)[1]
        start = date(year, month_num, 1)
        end = date(year, month_num, last_day)
        if end < from_date or start > to_date:
            continue
        start = max(start, from_date)
        end = min(end, to_date)
        best = (start, end)
    return best


def build_month_summary(
    company_name: str,
    period_from: date,
    period_to: date,
    group_parents: Optional[dict[str, str]] = None,
) -> dict[str, Any]:
    """
    Build Inflow/Outflow category lines for one month from local daybook,
    rolled up to Tally primary groups.
    """
    empty = {
        "from_date": period_from.isoformat(),
        "to_date": period_to.isoformat(),
        "inflow_lines": [],
        "outflow_lines": [],
        "total_inflow": 0.0,
        "total_outflow": 0.0,
        "nett": 0.0,
    }
    if group_parents is None:
        group_parents = fetch_group_parents(company_name)

    conn = sqlite3.connect(get_db_path())
    conn.row_factory = sqlite3.Row
    try:
        exists = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='tally_daybook'"
        ).fetchone()
        if not exists:
            return empty

        ledger_types = _load_ledger_types(conn, company_name)
        rows = conn.execute(
            """
            SELECT date, voucher_number, voucher_type, ledger_name, amount, is_debit
            FROM tally_daybook
            WHERE company_name = ?
              AND date >= ? AND date <= ?
            """,
            (company_name, period_from.isoformat(), period_to.isoformat()),
        ).fetchall()
    finally:
        conn.close()

    vouchers: dict[tuple, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        key = (r["date"], r["voucher_number"] or "", r["voucher_type"] or "")
        amt = float(r["amount"] or 0)
        signed = -amt if _is_debit(r["is_debit"]) else amt
        vouchers[key].append({"name": r["ledger_name"] or "", "amount": signed})

    inflow: dict[str, float] = defaultdict(float)
    outflow: dict[str, float] = defaultdict(float)

    for ents in vouchers.values():
        if not any(_is_cash(e["name"], ledger_types) for e in ents):
            continue
        for e in ents:
            name = e["name"]
            if not name or _is_cash(name, ledger_types):
                continue
            amt = float(e["amount"] or 0)
            cat = _category(name, ledger_types, group_parents)
            if amt > 0:
                inflow[cat] += amt
            elif amt < 0:
                outflow[cat] += -amt

    inflow_lines = [
        {"particulars": k, "amount": round(v, 2)}
        for k, v in sorted(inflow.items(), key=lambda x: x[0].lower())
        if abs(v) >= 0.005
    ]
    outflow_lines = [
        {"particulars": k, "amount": round(v, 2)}
        for k, v in sorted(outflow.items(), key=lambda x: x[0].lower())
        if abs(v) >= 0.005
    ]
    total_in = round(sum(inflow.values()), 2)
    total_out = round(sum(outflow.values()), 2)
    return {
        "from_date": period_from.isoformat(),
        "to_date": period_to.isoformat(),
        "inflow_lines": inflow_lines,
        "outflow_lines": outflow_lines,
        "total_inflow": total_in,
        "total_outflow": total_out,
        "nett": round(total_in - total_out, 2),
    }


def refresh_reserved_pnl_ledger_types(company_name: str) -> int:
    """
    Re-read Direct/Indirect Expenses & Incomes ledger parents from Tally into
    local customers.ledger_type. Repairs older syncs that collapsed any
    '*expense*' parent into Indirect Expenses.
    """
    config = load_config()
    updated = 0
    conn = sqlite3.connect(get_db_path())
    try:
        exists = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='customers'"
        ).fetchone()
        if not exists:
            return 0
        for parent in (
            "Direct Expenses",
            "Indirect Expenses",
            "Direct Incomes",
            "Indirect Incomes",
        ):
            xml = f"""
<ENVELOPE>
 <HEADER><VERSION>1</VERSION><TALLYREQUEST>Export</TALLYREQUEST>
 <TYPE>Collection</TYPE><ID>CFPnLLeds</ID></HEADER>
 <BODY><DESC>
  <STATICVARIABLES>
   <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
   <SVCURRENTCOMPANY>{escape(company_name)}</SVCURRENTCOMPANY>
  </STATICVARIABLES>
  <TDL><TDLMESSAGE>
   <COLLECTION NAME="CFPnLLeds">
    <TYPE>Ledger</TYPE>
    <FETCH>NAME, PARENT</FETCH>
    <FILTER>CFPnLFilter</FILTER>
   </COLLECTION>
   <SYSTEM TYPE="Formulae" NAME="CFPnLFilter">$$IsEqual:$Parent:"{escape(parent)}"</SYSTEM>
  </TDLMESSAGE></TDL>
 </DESC></BODY>
</ENVELOPE>"""
            try:
                response = requests.post(
                    config["tally_url"],
                    data=xml.encode("utf-8"),
                    headers={"Content-Type": "application/xml", "Connection": "close"},
                    timeout=tally_http_timeout(local_read=60, remote_read=120),
                )
                response.raise_for_status()
                text = clean_source_xml(decode_response(response))
            except Exception as e:
                logger.warning(f"CF P&L ledger refresh failed for {parent}: {e}")
                continue
            names = [
                html.unescape(n).strip()
                for n in re.findall(r'<LEDGER NAME="([^"]+)"', text)
                if n.strip()
            ]
            for name in names:
                cur = conn.execute(
                    "UPDATE customers SET ledger_type = ? "
                    "WHERE company_name = ? AND customer_name = ? "
                    "AND IFNULL(ledger_type, '') != ?",
                    (parent, company_name, name, parent),
                )
                updated += cur.rowcount
        conn.commit()
    finally:
        conn.close()
    if updated:
        logger.info(
            f"Cash Flow Summary: repaired {updated} ledger_type row(s) "
            f"for {company_name}"
        )
    return updated


def attach_summaries_to_months(
    company_name: str,
    months: list[dict[str, Any]],
    from_date: date,
    to_date: date,
) -> list[dict[str, Any]]:
    """Enrich each monthly Cash Flow row with a Summary drill-down payload."""
    try:
        refresh_reserved_pnl_ledger_types(company_name)
    except Exception as e:
        logger.warning(f"CF ledger_type repair skipped: {e}")
    group_parents = fetch_group_parents(company_name)
    enriched: list[dict[str, Any]] = []
    for m in months:
        row = dict(m)
        label = str(row.get("period") or "")
        rng = month_date_range(label, from_date, to_date)
        if rng:
            summary = build_month_summary(
                company_name, rng[0], rng[1], group_parents=group_parents
            )
            # Prefer Tally monthly totals for the Summary header/footer when present.
            summary["total_inflow"] = round(
                float(row.get("inflow") or summary["total_inflow"]), 2
            )
            summary["total_outflow"] = round(
                float(row.get("outflow") or summary["total_outflow"]), 2
            )
            summary["nett"] = round(
                summary["total_inflow"] - summary["total_outflow"], 2
            )
            row["summary"] = summary
            row["year"] = rng[0].year
            row["month"] = rng[0].month
        else:
            row["summary"] = None
        enriched.append(row)
    return enriched
