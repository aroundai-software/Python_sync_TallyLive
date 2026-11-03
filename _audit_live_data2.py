"""Continue live GlobeMate audit (BS without from_date col)."""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import date

from supabase import create_client

from src.config import SUPABASE_URL, SUPABASE_SERVICE_KEY
from src.date_utils import company_period_bounds, indian_fy_end_for

sb = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

print("=== balance_sheet_reports columns probe ===")
bs = (
    sb.table("balance_sheet_reports")
    .select("*")
    .ilike("company_name", "%GlobeMate%")
    .limit(1)
    .execute()
    .data
    or []
)
if bs:
    print("keys:", sorted(bs[0].keys()))

print("\n=== balance_sheet_reports ===")
for row in (
    sb.table("balance_sheet_reports")
    .select(
        "company_name,company_guid,as_on_date,total_liabilities,total_assets,synced_at"
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
    from_d = row.get("from_date")
    outside = []
    if from_d and isinstance(months, list):
        try:
            fy_end = indian_fy_end_for(date.fromisoformat(str(from_d)[:10]))
            for m in months:
                if not isinstance(m, dict):
                    continue
                s = m.get("summary") if isinstance(m.get("summary"), dict) else {}
                mt = (s or {}).get("to_date") or (s or {}).get("from_date")
                if mt and str(mt)[:10] > fy_end.isoformat():
                    outside.append(f"{m.get('period')}:{mt}")
        except Exception as e:
            outside.append(f"err:{e}")
    print(
        json.dumps(
            {
                "company_name": row.get("company_name"),
                "from_date": row.get("from_date"),
                "to_date": row.get("to_date"),
                "total_inflow": row.get("total_inflow"),
                "total_outflow": row.get("total_outflow"),
                "total_nett": row.get("total_nett"),
                "synced_at": row.get("synced_at"),
                "month_count": len(months) if isinstance(months, list) else None,
                "month_periods": [
                    m.get("period") for m in (months or []) if isinstance(m, dict)
                ],
                "months_past_fy_of_from_date": outside,
            },
            default=str,
        )
    )
    # Expected if books_from known
    name = row.get("company_name")
    co = (
        sb.table("tally_companies")
        .select("books_from")
        .eq("company_name", name)
        .limit(1)
        .execute()
        .data
        or []
    )
    bf = co[0]["books_from"] if co else None
    start, end = company_period_bounds(date.today(), bf)
    print(f"  expected with books_from={bf}: {start} .. {end}")

print("\n=== sales/purchase/daybook date span ===")
for table, date_col in (
    ("sales_invoices", "invoice_date"),
    ("purchase_invoices", "invoice_date"),
    ("tally_daybook", "date"),
):
    # Use RPC-less approach: fetch distinct company rows with head count via range
    companies = (
        sb.table("tally_companies")
        .select("company_name,books_from")
        .ilike("company_name", "%GlobeMate%")
        .execute()
        .data
        or []
    )
    print(f"-- {table} --")
    for c in companies:
        name = c["company_name"]
        rows = (
            sb.table(table)
            .select(date_col)
            .eq("company_name", name)
            .order(date_col, desc=False)
            .limit(1)
            .execute()
            .data
            or []
        )
        rows2 = (
            sb.table(table)
            .select(date_col)
            .eq("company_name", name)
            .order(date_col, desc=True)
            .limit(1)
            .execute()
            .data
            or []
        )
        # count via exact prefer if available - approximate with select id limit
        cnt = (
            sb.table(table)
            .select("guid", count="exact")
            .eq("company_name", name)
            .limit(1)
            .execute()
        )
        print(
            f"  {name!r}: count={cnt.count} "
            f"min={rows[0].get(date_col) if rows else None} "
            f"max={rows2[0].get(date_col) if rows2 else None} "
            f"books_from={c.get('books_from')}"
        )

print("\n=== P&L vs company books_from mismatch summary ===")
pls = (
    sb.table("profit_loss_reports")
    .select("company_name,from_date,to_date,nett_profit,nett_loss,synced_at")
    .ilike("company_name", "%GlobeMate%")
    .execute()
    .data
    or []
)
cos = {
    c["company_name"]: c
    for c in (
        sb.table("tally_companies")
        .select("company_name,books_from")
        .ilike("company_name", "%GlobeMate%")
        .execute()
        .data
        or []
    )
}
for p in pls:
    bf = (cos.get(p["company_name"]) or {}).get("books_from")
    start, end = company_period_bounds(date.today(), bf)
    ok_from = str(p["from_date"])[:10] == start.isoformat()
    ok_to = str(p["to_date"])[:10] <= end.isoformat()
    print(
        f"{p['company_name']}: stored {p['from_date']}..{p['to_date']} "
        f"expected {start}..{end} from_ok={ok_from} to_ok={ok_to} "
        f"nett_p={p.get('nett_profit')} nett_l={p.get('nett_loss')}"
    )
