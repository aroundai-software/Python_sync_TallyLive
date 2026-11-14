"""Read-only: why only Vadakkethil is syncing (correct last_seen_at)."""
from datetime import datetime, timezone

from supabase import create_client

from src.config import load_config


def pdt(v):
    if not v:
        return None
    dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


sb = create_client(load_config()["supabase_url"], load_config()["supabase_key"])
now = datetime.now(timezone.utc)
sc = (
    sb.table("script_control")
    .select(
        "machine_name,sync_should_run,expires_at,app_version,last_seen_at,current_company"
    )
    .execute()
    .data
    or []
)

print("machine | should_run | version | licence | last_seen_at | age_h | current_company")
for r in sorted(
    sc, key=lambda x: x.get("last_seen_at") or "0000", reverse=True
):
    exp = pdt(r.get("expires_at"))
    last = pdt(r.get("last_seen_at"))
    if r.get("expires_at") in (None, ""):
        licence = "NO_EXPIRY"
    elif exp and exp < now:
        licence = "EXPIRED"
    else:
        licence = "OK"
    age = None if not last else round((now - last).total_seconds() / 3600, 1)
    print(
        r["machine_name"],
        "|",
        r["sync_should_run"],
        "|",
        r.get("app_version"),
        "|",
        licence,
        "|",
        r.get("last_seen_at"),
        "|",
        age,
        "|",
        r.get("current_company"),
    )

print("\n=== LIVE <6h ===")
for r in sc:
    last = pdt(r.get("last_seen_at"))
    if last and (now - last).total_seconds() < 6 * 3600:
        print(
            r["machine_name"],
            "v=",
            r.get("app_version"),
            "should_run=",
            r["sync_should_run"],
            "company=",
            r.get("current_company"),
            "age_h=",
            round((now - last).total_seconds() / 3600, 2),
        )

print("\n=== ENABLED but offline / quiet (>6h) ===")
for r in sorted(sc, key=lambda x: x.get("machine_name") or ""):
    if r.get("sync_should_run") is not True:
        continue
    last = pdt(r.get("last_seen_at"))
    age = None if not last else round((now - last).total_seconds() / 3600, 1)
    if last and (now - last).total_seconds() < 6 * 3600:
        continue
    exp = pdt(r.get("expires_at"))
    if r.get("expires_at") in (None, ""):
        licence = "NO_EXPIRY"
    elif exp and exp < now:
        licence = "EXPIRED"
    else:
        licence = "OK"
    print(
        r["machine_name"],
        "v=",
        r.get("app_version"),
        "licence=",
        licence,
        "expires=",
        r.get("expires_at"),
        "age_h=",
        age,
        "company=",
        r.get("current_company"),
    )

print("\n=== PAUSED (sync_should_run=false) that synced recently (versioned completed) ===")
paused = {r["machine_name"]: r for r in sc if r.get("sync_should_run") is not True}
logs = (
    sb.table("sync_logs")
    .select("machine_name,company_name,app_version,updated_at,status")
    .not_.is_("app_version", "null")
    .eq("status", "completed")
    .order("updated_at", desc=True)
    .limit(300)
    .execute()
    .data
    or []
)
seen = set()
for L in logs:
    m = L.get("machine_name")
    if m not in paused or m in seen:
        continue
    seen.add(m)
    r = paused[m]
    print(
        m,
        "company=",
        L.get("company_name"),
        "last_ok=",
        L.get("updated_at"),
        "v=",
        L.get("app_version"),
        "expires=",
        r.get("expires_at"),
        "last_seen_at=",
        r.get("last_seen_at"),
    )
