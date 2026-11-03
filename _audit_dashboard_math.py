"""Cross-check dashboard-ish figures for GlobeMate 2024-2025."""
from __future__ import annotations

import json
from supabase import create_client
from src.config import SUPABASE_URL, SUPABASE_SERVICE_KEY

sb = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
NAME = "S & D GlobeMate  (2024-2025)"

# outstanding schema
or_sample = (
    sb.table("outstanding_receivables").select("*").eq("company_name", NAME).limit(1).execute().data
    or []
)
print("OR keys", list(or_sample[0].keys()) if or_sample else None)
op_sample = (
    sb.table("outstanding_payables").select("*").eq("company_name", NAME).limit(1).execute().data
    or []
)
print("OP keys", list(op_sample[0].keys()) if op_sample else None)

# cash/bank
banks = (
    sb.table("customers")
    .select("*")
    .eq("company_name", NAME)
    .limit(1)
    .execute()
    .data
    or []
)
print("customer keys", list(banks[0].keys()) if banks else None)

# try ledger types
for lt in ["Cash-in-Hand", "Bank Accounts", "Bank OD A/c"]:
    rows = (
        sb.table("customers")
        .select("customer_name,ledger_type,closing_balance")
        .eq("company_name", NAME)
        .ilike("ledger_type", f"%{lt.split()[0]}%")
        .limit(50)
        .execute()
        .data
        or []
    )
    print(lt, "n", len(rows), "sum", round(sum(float(r.get("closing_balance") or 0) for r in rows), 2))

# stock keys + sum
st = sb.table("stock_items").select("*").eq("company_name", NAME).limit(1).execute().data or []
print("stock keys", list(st[0].keys()) if st else None)

# P&L lines
pl = (
    sb.table("profit_loss_reports")
    .select("id,from_date,to_date,gross_profit,gross_loss,nett_profit,nett_loss,synced_at")
    .eq("company_name", NAME)
    .maybe_single()
    .execute()
    .data
)
print("PL header", pl)
if pl:
    lines = (
        sb.table("profit_loss_lines")
        .select("particulars,amount,section,sort_order")
        .eq("report_id", pl["id"])
        .order("sort_order")
        .execute()
        .data
        or []
    )
    for l in lines:
        print(f"  [{l.get('section')}] {l.get('particulars')} = {l.get('amount')}")

# BS
bs = (
    sb.table("balance_sheet_reports")
    .select("id,as_on_date,total_liabilities,total_assets,synced_at")
    .eq("company_name", NAME)
    .maybe_single()
    .execute()
    .data
)
print("BS header", bs)
if bs:
    lines = (
        sb.table("balance_sheet_lines")
        .select("particulars,amount,section,sort_order")
        .eq("report_id", bs["id"])
        .order("sort_order")
        .limit(30)
        .execute()
        .data
        or []
    )
    for l in lines:
        print(f"  [{l.get('section')}] {l.get('particulars')} = {l.get('amount')}")

# cash flow months
cf = (
    sb.table("tally_cash_flow")
    .select("from_date,to_date,total_inflow,total_outflow,total_nett,months,synced_at")
    .eq("company_name", NAME)
    .maybe_single()
    .execute()
    .data
)
months = cf.get("months") if cf else []
if isinstance(months, str):
    months = json.loads(months)
print("\nCF header", {k: cf[k] for k in cf if k != "months"} if cf else None)
print("CF months with summary dates:")
visible = 0
hidden = 0
from datetime import date
from src.date_utils import indian_fy_end_for
fy_end = indian_fy_end_for(date.fromisoformat(cf["from_date"][:10]))
for m in months or []:
    s = m.get("summary") if isinstance(m, dict) else None
    s = s if isinstance(s, dict) else {}
    mt = (s.get("to_date") or s.get("from_date") or "")[:10]
    past = bool(mt and mt > fy_end.isoformat())
    if past:
        hidden += 1
    else:
        visible += 1
    print(
        f"  {m.get('period')}: in={m.get('inflow')} out={m.get('outflow')} nett={m.get('nett')} "
        f"dates={s.get('from_date')}..{s.get('to_date')} FILTER_OUT={past}"
    )
print(f"App filter would keep {visible} months, hide {hidden} months (fy_end={fy_end})")

# sales today
sales_today = (
    sb.table("sales_invoices")
    .select("guid", count="exact")
    .eq("company_name", NAME)
    .gte("invoice_date", "2026-11-03")
    .lte("invoice_date", "2026-11-03")
    .execute()
)
print("sales today count", sales_today.count)
sales_span = (
    sb.table("sales_invoices")
    .select("total_amount")
    .eq("company_name", NAME)
    .limit(5000)
    .execute()
    .data
    or []
)
# amount column probe
s1 = sb.table("sales_invoices").select("*").eq("company_name", NAME).limit(1).execute().data or []
print("sales keys", list(s1[0].keys()) if s1 else None)
