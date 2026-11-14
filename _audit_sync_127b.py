"""Deeper read-only look at recent sync activity after 1.2.7."""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta

from supabase import create_client

from src.config import load_config


def parse_dt(v):
    if not v:
        return None
    try:
        dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def main() -> None:
    sb = create_client(load_config()["supabase_url"], load_config()["supabase_key"])
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=7)

    # Pull more sync_logs without relying on created_at filter alone
    logs = (
        sb.table("sync_logs")
        .select("*")
        .order("updated_at", desc=True)
        .limit(1000)
        .execute()
        .data
        or []
    )
    print("sync_logs fetched", len(logs))
    if logs:
        print("cols", sorted(logs[0].keys()))

    recent = []
    for L in logs:
        dt = parse_dt(
            L.get("updated_at")
            or L.get("finished_at")
            or L.get("started_at")
            or L.get("synced_at")
            or L.get("created_at")
        )
        if dt and dt >= since:
            recent.append((dt, L))
    recent.sort(key=lambda x: x[0], reverse=True)
    print(f"logs in last 7 days: {len(recent)}")

    by_status = Counter()
    by_company = Counter()
    by_machine = Counter()
    by_version = Counter()
    completed_companies = set()
    failed_rows = []
    for dt, L in recent:
        st = str(L.get("status") or "")
        by_status[st] += 1
        co = L.get("company_name") or "(none)"
        by_company[co] += 1
        by_machine[L.get("machine_name") or "(none)"] += 1
        by_version[L.get("app_version") or "(none)"] += 1
        if st == "completed":
            completed_companies.add(co)
        if st in {"failed", "error", "partial"}:
            failed_rows.append((dt, L))

    print("status:", dict(by_status))
    print("versions:", dict(by_version))
    print("machines with any log:", dict(by_machine.most_common()))
    print("companies with any log:", dict(by_company.most_common(40)))
    print("companies with COMPLETED in 7d:", sorted(c for c in completed_companies if c != "(none)"))

    print("\n=== failed/partial last 7d (newest 30) ===")
    for dt, L in failed_rows[:30]:
        print(
            {
                "at": dt.isoformat(),
                "machine": L.get("machine_name"),
                "company": L.get("company_name"),
                "version": L.get("app_version"),
                "status": L.get("status"),
                "error_message": str(
                    L.get("error_message")
                    or L.get("error")
                    or L.get("message")
                    or L.get("details")
                    or L.get("error_details")
                    or ""
                )[:220],
                "steps_failed": L.get("steps_failed") or L.get("failed_steps"),
                "duration": L.get("duration_seconds") or L.get("duration"),
            }
        )

    print("\n=== completed last 7d by company/machine ===")
    completed = [(dt, L) for dt, L in recent if L.get("status") == "completed"]
    by_cm = defaultdict(list)
    for dt, L in completed:
        by_cm[(L.get("machine_name"), L.get("company_name"))].append(dt)
    for (m, c), times in sorted(by_cm.items(), key=lambda x: max(x[1]), reverse=True):
        print(f"  {m} | {c} | count={len(times)} | last={max(times).isoformat()}")

    # Data freshness for top companies from OFFICE vs others
    print("\n=== company master freshness (top 15 by updated_at) ===")
    cos = (
        sb.table("tally_companies")
        .select("company_name,books_from,updated_at,is_active")
        .eq("is_active", True)
        .order("updated_at", desc=True)
        .limit(15)
        .execute()
        .data
        or []
    )
    for row in cos:
        print(row)

    # Check if sales_invoices recently updated for a few companies
    print("\n=== recent sales_invoices activity (sample companies) ===")
    sample = [
        "Vadakkethil Sales And Marketing 2026-27",
        "S & D GlobeMate  (2026-2027)",
        "V.K TRADING CORPORATION - (from 1-Apr-26)",
        "NEXORA",
        "GROUP LAVENDER GLOBER 26-27",
    ]
    for name in sample:
        try:
            rows = (
                sb.table("sales_invoices")
                .select("invoice_date,updated_at,company_name")
                .eq("company_name", name)
                .order("updated_at", desc=True)
                .limit(1)
                .execute()
                .data
                or []
            )
            cnt = (
                sb.table("sales_invoices")
                .select("id", count="exact")
                .eq("company_name", name)
                .limit(1)
                .execute()
            )
            print(
                name,
                "count~",
                cnt.count,
                "latest_updated",
                rows[0] if rows else None,
            )
        except Exception as e:
            print(name, "ERR", e)

    print("\n=== script_control freshness buckets ===")
    machines = sb.table("script_control").select("*").execute().data or []
    buckets = Counter()
    for m in machines:
        ls = parse_dt(m.get("last_seen_at"))
        if not ls:
            buckets["no_last_seen"] += 1
        else:
            age_h = (now - ls.astimezone(timezone.utc)).total_seconds() / 3600
            if age_h < 6:
                buckets["seen_<6h"] += 1
            elif age_h < 48:
                buckets["seen_<48h"] += 1
            elif age_h < 168:
                buckets["seen_<7d"] += 1
            else:
                buckets["seen_>7d"] += 1
    print(dict(buckets))

    live = []
    for m in machines:
        ls = parse_dt(m.get("last_seen_at"))
        if ls and (now - ls.astimezone(timezone.utc)).total_seconds() < 6 * 3600:
            live.append(m)
    print("\nCurrently live (<6h last_seen):", len(live))
    for m in live:
        print(
            {
                "machine": m.get("machine_name"),
                "company": m.get("current_company"),
                "run": m.get("sync_should_run"),
                "ver": m.get("app_version"),
                "expires": m.get("expires_at"),
                "last_seen": m.get("last_seen_at"),
            }
        )


if __name__ == "__main__":
    main()
