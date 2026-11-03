"""Audit live GlobeMate report snapshots vs expected FY clamp."""
from __future__ import annotations

import json
from datetime import date

from supabase import create_client

from src.config import SUPABASE_URL, SUPABASE_SERVICE_KEY
from src.date_utils import company_period_bounds, indian_fy_end_for

sb = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

print("=== tally_companies (GlobeMate) ===")
companies = (
    sb.table("tally_companies")
    .select("company_name,guid,company_number,books_from,financial_year_from,updated_at,is_active")
    .ilike("company_name", "%GlobeMate%")
    .execute()
    .data
    or []
)
for c in companies:
    print(json.dumps(c, default=str))

print("\n=== profit_loss_reports ===")
for row in (
    sb.table("profit_loss_reports")
    .select(
        "company_name,company_guid,from_date,to_date,gross_profit,gross_loss,nett_profit,nett_loss,synced_at"
    )
    .ilike("company_name", "%GlobeMate%")
    .execute()
    .data
    or []
):
    print(json.dumps(row, default=str))
    bf = next(
        (
            c.get("books_from")
            for c in companies
            if c.get("company_name") == row.get("company_name")
        ),
        None,
    )
    if bf or row.get("from_date"):
        start, end = company_period_bounds(
            as_on=date.today(),
            books_from_iso=bf or row.get("from_date"),
        )
        print(
            f"  expected_bounds books_from={bf} -> {start} .. {end} | "
            f"stored={row.get('from_date')} .. {row.get('to_date')} | "
            f"to_date_past_fy={str(row.get('to_date')) > end.isoformat() if row.get('to_date') else '?'}"
        )

print("\n=== balance_sheet_reports ===")
for row in (
    sb.table("balance_sheet_reports")
    .select(
        "company_name,company_guid,from_date,to_date,as_on_date,total_liabilities,total_assets,synced_at"
    )
    .ilike("company_name", "%GlobeMate%")
    .execute()
    .data
    or []
):
    print(json.dumps(row, default=str))

print("\n=== tally_ratio_analysis ===")
for row in (
    sb.table("tally_ratio_analysis")
    .select("company_name,from_date,to_date,recv_turnover_days,synced_at")
    .ilike("company_name", "%GlobeMate%")
    .execute()
    .data
    or []
):
    print(json.dumps(row, default=str))

print("\n=== tally_cash_flow ===")
for row in (
    sb.table("tally_cash_flow")
    .select(
        "company_name,from_date,to_date,total_inflow,total_outflow,total_nett,synced_at,months"
    )
    .ilike("company_name", "%GlobeMate%")
    .execute()
    .data
    or []
):
    months = row.get("months") or []
    if isinstance(months, str):
        months = json.loads(months)
    summary = {
        **{k: row[k] for k in row if k != "months"},
        "month_count": len(months) if isinstance(months, list) else None,
        "month_periods": [
            m.get("period") for m in (months or []) if isinstance(m, dict)
        ][:20],
    }
    # Check months outside FY of from_date
    from_d = row.get("from_date")
    outside = []
    if from_d and isinstance(months, list):
        try:
            fy_end = indian_fy_end_for(date.fromisoformat(str(from_d)[:10]))
            for m in months:
                if not isinstance(m, dict):
                    continue
                s = (m.get("summary") or {}) if isinstance(m.get("summary"), dict) else {}
                mt = s.get("to_date") or s.get("from_date")
                if mt and str(mt)[:10] > fy_end.isoformat():
                    outside.append(f"{m.get('period')}:{mt}")
        except Exception as e:
            outside.append(f"err:{e}")
    summary["months_past_fy_end"] = outside
    print(json.dumps(summary, default=str))

print("\n=== voucher counts by company ===")
for table, date_col in (
    ("sales_invoices", "invoice_date"),
    ("purchase_invoices", "invoice_date"),
    ("tally_daybook", "date"),
):
    rows = (
        sb.table(table)
        .select(f"company_name,{date_col}")
        .ilike("company_name", "%GlobeMate%")
        .limit(5000)
        .execute()
        .data
        or []
    )
    by = {}
    for r in rows:
        name = r.get("company_name") or "?"
        d = str(r.get(date_col) or "")[:7]
        by.setdefault(name, {"count": 0, "months": {}})
        by[name]["count"] += 1
        by[name]["months"][d] = by[name]["months"].get(d, 0) + 1
    print(f"-- {table} (sample up to 5000) --")
    for name, info in by.items():
        months_sorted = sorted(info["months"].items())
        print(
            f"  {name!r}: n={info['count']} first={months_sorted[:3]} last={months_sorted[-3:]}"
        )
