"""Read-only audit of live sync / subscriptions after 1.2.7. Do not mutate."""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone, timedelta

from supabase import create_client

from src.config import load_config


def main() -> None:
    c = load_config()
    sb = create_client(c["supabase_url"], c["supabase_key"])
    print("project", c["supabase_url"])
    print("service_role", bool(c.get("supabase_using_service_role")))
    print()

    # --- Active release ---
    print("=== sync_app_releases (active) ===")
    rel = (
        sb.table("sync_app_releases")
        .select("version,is_active,storage_path,sha256,released_at,notes,mandatory")
        .eq("is_active", True)
        .execute()
        .data
    )
    for r in rel:
        print(r)
    print()

    # --- script_control (subscriptions / machines) ---
    print("=== script_control (machines / subscriptions) ===")
    machines = (
        sb.table("script_control")
        .select("*")
        .order("last_seen_at", desc=True)
        .execute()
        .data
        or []
    )
    print(f"count={len(machines)}")
    now = datetime.now(timezone.utc)
    for m in machines:
        exp = m.get("expires_at")
        exp_dt = None
        if exp:
            try:
                exp_dt = datetime.fromisoformat(str(exp).replace("Z", "+00:00"))
            except Exception:
                pass
        expired = bool(exp_dt and now > exp_dt)
        print(
            {
                "machine": m.get("machine_name"),
                "id": m.get("id"),
                "current_company": m.get("current_company"),
                "sync_should_run": m.get("sync_should_run"),
                "is_active": m.get("is_active"),
                "app_version": m.get("app_version"),
                "expires_at": exp,
                "expired": expired,
                "last_seen_at": m.get("last_seen_at"),
                "last_error": (m.get("last_error") or m.get("error") or "")[:120],
                "status": m.get("status") or m.get("sync_status"),
            }
        )
    print()

    # --- machine_companies links ---
    print("=== machine_companies ===")
    links = sb.table("machine_companies").select("*").execute().data or []
    print(f"count={len(links)}")
    by_machine: dict[str, list[str]] = {}
    for row in links:
        mn = (row.get("machine_name") or "").strip()
        cn = (row.get("company_name") or "").strip()
        by_machine.setdefault(mn, []).append(cn)
    for mn, cos in sorted(by_machine.items()):
        print(f"  {mn}: {cos}")
    print()

    # --- tally_companies ---
    print("=== tally_companies (active) ===")
    cos = (
        sb.table("tally_companies")
        .select(
            "company_name,guid,company_number,books_from,is_active,sync_enabled,updated_at"
        )
        .eq("is_active", True)
        .order("updated_at", desc=True)
        .execute()
        .data
        or []
    )
    print(f"active_count={len(cos)}")
    for row in cos[:30]:
        print(row)
    if len(cos) > 30:
        print(f"  ... +{len(cos) - 30} more")
    print()

    # --- sync_logs recent ---
    print("=== sync_logs (last 48h summary) ===")
    since = (now - timedelta(hours=48)).isoformat()
    try:
        logs = (
            sb.table("sync_logs")
            .select("*")
            .gte("created_at", since)
            .order("created_at", desc=True)
            .limit(500)
            .execute()
            .data
            or []
        )
    except Exception as e:
        print("sync_logs query failed:", e)
        # try alternate timestamp column
        try:
            logs = (
                sb.table("sync_logs")
                .select("*")
                .order("id", desc=True)
                .limit(200)
                .execute()
                .data
                or []
            )
            print(f"fallback fetched {len(logs)} rows by id")
        except Exception as e2:
            print("fallback failed:", e2)
            logs = []

    if logs:
        print(f"rows={len(logs)} sample_cols={list(logs[0].keys())}")
        by_company = Counter()
        by_machine = Counter()
        by_version = Counter()
        by_status = Counter()
        errors = []
        for L in logs:
            by_company[L.get("company_name") or L.get("current_company") or "(none)"] += 1
            by_machine[L.get("machine_name") or "(none)"] += 1
            by_version[L.get("app_version") or "(none)"] += 1
            st = L.get("status") or L.get("sync_status") or L.get("result") or "(none)"
            by_status[str(st)] += 1
            msg = (
                L.get("error_message")
                or L.get("error")
                or L.get("message")
                or L.get("details")
                or ""
            )
            if st and str(st).lower() in {"failed", "error", "fail"} or (
                msg and "error" in str(msg).lower()
            ):
                errors.append(
                    {
                        "at": L.get("created_at") or L.get("synced_at") or L.get("started_at"),
                        "company": L.get("company_name"),
                        "machine": L.get("machine_name"),
                        "version": L.get("app_version"),
                        "status": st,
                        "msg": str(msg)[:200],
                    }
                )

        print("by_status:", dict(by_status.most_common()))
        print("by_version:", dict(by_version.most_common()))
        print("by_machine:", dict(by_machine.most_common(20)))
        print("by_company:", dict(by_company.most_common(30)))
        print(f"error-ish rows={len(errors)}")
        for e in errors[:25]:
            print("  ERR", e)
        print()
        print("=== sync_logs newest 15 ===")
        for L in logs[:15]:
            print(
                {
                    "at": L.get("created_at") or L.get("synced_at") or L.get("started_at"),
                    "company": L.get("company_name"),
                    "machine": L.get("machine_name"),
                    "version": L.get("app_version"),
                    "status": L.get("status") or L.get("sync_status"),
                    "step": L.get("step") or L.get("phase"),
                    "msg": str(
                        L.get("error_message")
                        or L.get("message")
                        or L.get("details")
                        or ""
                    )[:160],
                }
            )
    else:
        print("no sync_logs rows found")

    # --- Cross-check: machines that should run vs recent company sync ---
    print()
    print("=== ISSUE SCAN ===")
    should_run = [m for m in machines if m.get("sync_should_run") is True]
    print(f"machines sync_should_run=true: {len(should_run)}")
    for m in should_run:
        print(
            "  RUN",
            m.get("machine_name"),
            "company=",
            m.get("current_company"),
            "ver=",
            m.get("app_version"),
            "last_seen=",
            m.get("last_seen_at"),
        )

    paused = [m for m in machines if m.get("sync_should_run") is not True]
    print(f"machines paused/not-run: {len(paused)}")
    for m in paused:
        exp = m.get("expires_at")
        exp_dt = None
        if exp:
            try:
                exp_dt = datetime.fromisoformat(str(exp).replace("Z", "+00:00"))
            except Exception:
                pass
        print(
            "  PAUSE",
            m.get("machine_name"),
            "expired=",
            bool(exp_dt and now > exp_dt),
            "expires_at=",
            exp,
            "company=",
            m.get("current_company"),
            "ver=",
            m.get("app_version"),
            "last_seen=",
            m.get("last_seen_at"),
        )

    versions = Counter((m.get("app_version") or "(none)") for m in machines)
    print("machine app_version mix:", dict(versions))
    if rel and rel[0].get("version") == "1.2.7":
        not_updated = [
            m.get("machine_name")
            for m in machines
            if (m.get("app_version") or "") not in ("1.2.7", "")
            and m.get("app_version")
            and m.get("app_version") != "1.2.7"
        ]
        print("machines not on 1.2.7:", not_updated)


if __name__ == "__main__":
    main()
