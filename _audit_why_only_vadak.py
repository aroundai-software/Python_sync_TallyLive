"""Read-only: why only Vadakkethil is syncing now."""
from collections import defaultdict
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


sb = create_client(load_config()["supabase_url"], load_config()["supabase_key"])
now = datetime.now(timezone.utc)

# script_control snapshot
sc = (
    sb.table("script_control")
    .select("*")
    .execute()
    .data
    or []
)

print("=== script_control machines ===")
print(f"total={len(sc)}")

rows = []
for r in sc:
    last = parse_dt(r.get("last_seen") or r.get("updated_at"))
    exp = parse_dt(r.get("expires_at"))
    age_h = None if not last else (now - last).total_seconds() / 3600
    expired = False
    if exp is not None:
        expired = exp < now
    elif r.get("expires_at") in (None, ""):
        expired = True  # treat missing expiry as not licensed / paused typically
    rows.append(
        {
            "machine": r.get("machine_name"),
            "should_run": r.get("sync_should_run"),
            "version": r.get("app_version"),
            "expires_at": r.get("expires_at"),
            "expired": expired,
            "last_seen": r.get("last_seen"),
            "age_h": age_h,
            "paused_reason": r.get("paused_reason") or r.get("pause_reason") or r.get("status_reason"),
        }
    )

live = [r for r in rows if r["age_h"] is not None and r["age_h"] < 6]
day = [r for r in rows if r["age_h"] is not None and r["age_h"] < 24]
enabled = [r for r in rows if r["should_run"] is True]
paused = [r for r in rows if r["should_run"] is not True]

print(f"sync_should_run=true: {len(enabled)}")
print(f"paused/false: {len(paused)}")
print(f"last_seen < 6h: {len(live)}")
print(f"last_seen < 24h: {len(day)}")

print("\n--- LIVE now (<6h) ---")
for r in sorted(live, key=lambda x: x["age_h"] or 999):
    print(
        f"  {r['machine']} | should_run={r['should_run']} | v={r['version']} | "
        f"expires={r['expires_at']} | last_seen_h={None if r['age_h'] is None else round(r['age_h'], 2)}"
    )

print("\n--- ENABLED but NOT live (should_run=true, last_seen>=6h or never) ---")
en_offline = [
    r
    for r in enabled
    if r["age_h"] is None or r["age_h"] >= 6
]
for r in sorted(en_offline, key=lambda x: (x["age_h"] is None, -(x["age_h"] or -1))):
    print(
        f"  {r['machine']} | v={r['version']} | expires={r['expires_at']} | "
        f"expired={r['expired']} | last_seen={r['last_seen']} | "
        f"age_h={None if r['age_h'] is None else round(r['age_h'], 1)}"
    )

print("\n--- Recently versioned completers (from sync_logs) vs current control ---")
logs = (
    sb.table("sync_logs")
    .select("machine_name,company_name,status,app_version,updated_at")
    .not_.is_("app_version", "null")
    .eq("status", "completed")
    .order("updated_at", desc=True)
    .limit(500)
    .execute()
    .data
    or []
)
since = now - timedelta(days=7)
last_ok = {}
for L in logs:
    dt = parse_dt(L.get("updated_at"))
    if not dt or dt < since:
        continue
    key = (L.get("machine_name"), L.get("company_name"))
    if key not in last_ok:
        last_ok[key] = L

ctrl_by = {r["machine"]: r for r in rows}
print(f"distinct machine+company completed (7d, versioned): {len(last_ok)}")
by_why = defaultdict(list)
for (m, c), L in sorted(last_ok.items(), key=lambda x: x[1].get("updated_at") or "", reverse=True):
    ctrl = ctrl_by.get(m) or {}
    age = ctrl.get("age_h")
    should = ctrl.get("should_run")
    exp = ctrl.get("expires_at")
    expired = ctrl.get("expired")
    ver_now = ctrl.get("version")
    last_sync = L.get("updated_at")
    if should is True and age is not None and age < 6:
        why = "ONLINE_AND_ENABLED"
    elif should is not True:
        if expired:
            why = "PAUSED_EXPIRED_SUB"
        else:
            why = "PAUSED_OTHER"
    elif age is None:
        why = "ENABLED_NEVER_SEEN"
    elif age >= 24:
        why = "ENABLED_OFFLINE_>24h"
    else:
        why = "ENABLED_OFFLINE_6_24h"
    by_why[why].append((m, c, L.get("app_version"), last_sync, should, exp, ver_now, age))

for why, items in sorted(by_why.items()):
    print(f"\n{why} ({len(items)})")
    for m, c, v, last_sync, should, exp, ver_now, age in items[:40]:
        print(
            f"  machine={m} | company={c} | last_sync_v={v} | last_sync={last_sync} | "
            f"should_run={should} | expires={exp} | ctrl_v={ver_now} | "
            f"age_h={None if age is None else round(age, 1)}"
        )

# machine_companies for live/enabled
print("\n=== machine_companies for ENABLED machines ===")
mcs = (
    sb.table("machine_companies")
    .select("machine_name,company_name,is_active,updated_at")
    .execute()
    .data
    or []
)
mc_by = defaultdict(list)
for mc in mcs:
    if mc.get("is_active") is False:
        continue
    mc_by[mc.get("machine_name")].append(mc.get("company_name"))
for r in sorted(enabled, key=lambda x: x["machine"] or ""):
    cos = mc_by.get(r["machine"]) or []
    print(
        f"  {r['machine']} | should_run={r['should_run']} | v={r['version']} | "
        f"expires={r['expires_at']} | age_h={None if r['age_h'] is None else round(r['age_h'], 1)} | "
        f"companies={cos[:8]}{'...' if len(cos)>8 else ''}"
    )
