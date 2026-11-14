"""Read-only audit: SAMAGRA balance sheet zeros vs line detail."""
from collections import Counter, defaultdict
from datetime import datetime, timezone

from supabase import create_client

from src.config import load_config

COMPANY_LIKE = "%SAMAGRA%"


def pdt(v):
    if not v:
        return None
    dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


sb = create_client(load_config()["supabase_url"], load_config()["supabase_key"])
now = datetime.now(timezone.utc)

print("=== tally_companies matching SAMAGRA ===")
cos = (
    sb.table("tally_companies")
    .select("*")
    .ilike("company_name", COMPANY_LIKE)
    .execute()
    .data
    or []
)
for c in cos:
    print(
        {
            "name": c.get("company_name"),
            "guid": c.get("guid"),
            "company_number": c.get("company_number"),
            "books_from": c.get("books_from"),
            "books_to": c.get("books_to") or c.get("ending_from") or c.get("books_upto"),
            "is_active": c.get("is_active"),
            "updated_at": c.get("updated_at"),
            "machine": c.get("machine_name") or c.get("last_machine"),
        }
    )
    # print all keys once
print("company_keys_sample=", sorted(cos[0].keys()) if cos else None)

print("\n=== machine_companies for SAMAGRA ===")
mcs = (
    sb.table("machine_companies")
    .select("*")
    .ilike("company_name", COMPANY_LIKE)
    .execute()
    .data
    or []
)
machines = sorted({m.get("machine_name") for m in mcs if m.get("machine_name")})
for m in mcs:
    print(m)

print("\n=== script_control for those machines ===")
for mn in machines:
    sc = (
        sb.table("script_control")
        .select("*")
        .eq("machine_name", mn)
        .execute()
        .data
        or []
    )
    for r in sc:
        last = pdt(r.get("last_seen_at"))
        age = None if not last else round((now - last).total_seconds() / 3600, 2)
        print(
            {
                "machine": r.get("machine_name"),
                "should_run": r.get("sync_should_run"),
                "version": r.get("app_version"),
                "expires_at": r.get("expires_at"),
                "last_seen_at": r.get("last_seen_at"),
                "age_h": age,
                "current_company": r.get("current_company"),
            }
        )

# Also find machines whose current_company matches
print("\n=== script_control current_company like SAMAGRA ===")
sc_all = (
    sb.table("script_control")
    .select("*")
    .ilike("current_company", COMPANY_LIKE)
    .execute()
    .data
    or []
)
for r in sc_all:
    print(
        {
            "machine": r.get("machine_name"),
            "should_run": r.get("sync_should_run"),
            "version": r.get("app_version"),
            "expires_at": r.get("expires_at"),
            "last_seen_at": r.get("last_seen_at"),
            "current_company": r.get("current_company"),
        }
    )
    if r.get("machine_name") and r.get("machine_name") not in machines:
        machines.append(r.get("machine_name"))

print("\nmachines_to_check=", machines)

print("\n=== sync_logs for SAMAGRA / those machines (newest 40 versioned+all) ===")
logs = (
    sb.table("sync_logs")
    .select("*")
    .ilike("company_name", COMPANY_LIKE)
    .order("updated_at", desc=True)
    .limit(40)
    .execute()
    .data
    or []
)
print("samagra_named_logs=", len(logs))
for L in logs[:25]:
    print(
        {
            "at": L.get("updated_at"),
            "machine": L.get("machine_name"),
            "company": L.get("company_name"),
            "status": L.get("status"),
            "version": L.get("app_version"),
            "error": str(L.get("error_message") or "")[:180],
            "steps_failed": L.get("steps_failed") or L.get("failed_steps"),
        }
    )

for mn in machines:
    mlogs = (
        sb.table("sync_logs")
        .select("*")
        .eq("machine_name", mn)
        .order("updated_at", desc=True)
        .limit(30)
        .execute()
        .data
        or []
    )
    print(f"\n--- last 30 logs for machine {mn} ---")
    by_co = Counter()
    by_st = Counter()
    years = defaultdict(list)
    for L in mlogs:
        by_co[L.get("company_name") or "(null)"] += 1
        by_st[L.get("status")] += 1
        print(
            {
                "at": L.get("updated_at"),
                "company": L.get("company_name"),
                "status": L.get("status"),
                "version": L.get("app_version"),
                "error": str(L.get("error_message") or "")[:160],
            }
        )
    print("status=", dict(by_st))
    print("companies_in_recent_logs=", by_co.most_common())

    # All companies this machine has synced (versioned completed, broader pull)
    all_m = (
        sb.table("sync_logs")
        .select("company_name,status,app_version,updated_at")
        .eq("machine_name", mn)
        .order("updated_at", desc=True)
        .limit(500)
        .execute()
        .data
        or []
    )
    last_by_co = {}
    for L in all_m:
        co = L.get("company_name") or "(null)"
        if co not in last_by_co:
            last_by_co[co] = L
    print(f"\nDistinct companies in last 500 logs for {mn}: {len(last_by_co)}")
    for co, L in sorted(last_by_co.items(), key=lambda x: x[1].get("updated_at") or "", reverse=True):
        print(
            " ",
            co,
            "| last=",
            L.get("updated_at"),
            "| status=",
            L.get("status"),
            "| v=",
            L.get("app_version"),
        )

print("\n=== balance_sheet_reports for SAMAGRA ===")
bs = (
    sb.table("balance_sheet_reports")
    .select("*")
    .ilike("company_name", COMPANY_LIKE)
    .execute()
    .data
    or []
)
print("report_count=", len(bs))
for r in bs:
    print(
        {
            "id": r.get("id"),
            "company_name": r.get("company_name"),
            "company_guid": r.get("company_guid"),
            "from_date": r.get("from_date"),
            "as_on_date": r.get("as_on_date"),
            "synced_at": r.get("synced_at") or r.get("updated_at"),
            "total_liabilities": r.get("total_liabilities"),
            "total_assets": r.get("total_assets"),
            "keys": sorted(r.keys()),
        }
    )

# For each report, summarize lines
print("\n=== balance_sheet_lines summary per report ===")
for r in bs:
    rid = r.get("id")
    lines = (
        sb.table("balance_sheet_lines")
        .select("*")
        .eq("report_id", rid)
        .execute()
        .data
        or []
    )
    # fallback by company if report_id link differs
    if not lines:
        lines = (
            sb.table("balance_sheet_lines")
            .select("*")
            .eq("company_name", r.get("company_name"))
            .execute()
            .data
            or []
        )
    print(f"\nreport {rid} company={r.get('company_name')} lines={len(lines)}")
    if lines:
        print("line_keys=", sorted(lines[0].keys()))
    by_side = Counter()
    by_parent = Counter()
    by_type = Counter()
    zero_amt = 0
    nonzero = 0
    sample_top = []
    for ln in lines:
        side = ln.get("side") or ln.get("bs_side") or ln.get("section") or "?"
        parent = ln.get("parent") or ln.get("group_name") or ln.get("category") or "?"
        typ = ln.get("line_type") or ln.get("type") or ln.get("is_group")
        amt = ln.get("amount")
        if amt is None:
            amt = ln.get("closing_balance")
        if amt is None:
            amt = ln.get("debit")
        try:
            aval = float(amt or 0)
        except Exception:
            aval = 0
        by_side[side] += 1
        by_parent[str(parent)[:60]] += 1
        by_type[str(typ)] += 1
        if aval == 0:
            zero_amt += 1
        else:
            nonzero += 1
        sample_top.append((abs(aval), ln))
    print("by_side=", dict(by_side))
    print("by_type=", dict(by_type))
    print("zero_amt=", zero_amt, "nonzero=", nonzero)
    print("top_parents=", by_parent.most_common(20))
    sample_top.sort(key=lambda x: x[0], reverse=True)
    print("largest 15 lines:")
    for _, ln in sample_top[:15]:
        print(
            {
                k: ln.get(k)
                for k in (
                    "particulars",
                    "name",
                    "ledger_name",
                    "group_name",
                    "parent",
                    "side",
                    "section",
                    "amount",
                    "closing_balance",
                    "debit",
                    "credit",
                    "line_type",
                    "is_group",
                    "level",
                    "sort_order",
                )
                if k in ln
            }
        )

# Root-level groups specifically
print("\n=== root-ish Fixed Assets / Current Assets / Capital lines ===")
for r in bs:
    lines = (
        sb.table("balance_sheet_lines")
        .select("*")
        .eq("company_name", r.get("company_name"))
        .execute()
        .data
        or []
    )
    for ln in lines:
        name = (
            str(ln.get("particulars") or ln.get("name") or ln.get("ledger_name") or "")
        )
        if name.upper() in {
            "FIXED ASSETS",
            "CURRENT ASSETS",
            "CAPITAL ACCOUNT",
            "LOANS (LIABILITY)",
            "CURRENT LIABILITIES",
            "PROFIT & LOSS A/C",
            "LIABILITIES",
            "ASSETS",
        } or "FIXED ASSET" in name.upper() or name.upper().startswith("CURRENT ASSET"):
            print(ln)

print("\n=== other year companies on same machine(s) in tally_companies via machine_companies ===")
for mn in machines:
    mcos = (
        sb.table("machine_companies")
        .select("*")
        .eq("machine_name", mn)
        .execute()
        .data
        or []
    )
    print(f"machine={mn} linked_companies={len(mcos)}")
    for mc in sorted(mcos, key=lambda x: x.get("company_name") or ""):
        cn = mc.get("company_name")
        tc = (
            sb.table("tally_companies")
            .select("company_name,guid,company_number,books_from,updated_at,is_active")
            .eq("company_name", cn)
            .execute()
            .data
            or []
        )
        bs_n = (
            sb.table("balance_sheet_reports")
            .select("company_name,from_date,as_on_date,synced_at,updated_at,total_liabilities,total_assets")
            .eq("company_name", cn)
            .execute()
            .data
            or []
        )
        print(
            " ",
            cn,
            "| tc=",
            tc[:1],
            "| bs_reports=",
            bs_n,
        )
