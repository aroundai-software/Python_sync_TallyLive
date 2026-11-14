"""Read-only: sync_logs from app_version >= 1.2.5 + subscription health."""
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta

from supabase import create_client

from src.config import load_config


def pdt(v):
    if not v:
        return None
    dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def ver_tuple(v):
    try:
        parts = [int(x) for x in str(v).strip().split(".")[:3]]
        while len(parts) < 3:
            parts.append(0)
        return tuple(parts)
    except Exception:
        return None


MIN_VER = (1, 2, 5)
now = datetime.now(timezone.utc)
sb = create_client(load_config()["supabase_url"], load_config()["supabase_key"])

print("=== active release ===")
rel = (
    sb.table("sync_app_releases")
    .select("version,is_active,mandatory,released_at,notes,storage_path")
    .eq("is_active", True)
    .execute()
    .data
    or []
)
for r in rel:
    print(r)

# Pull recent sync_logs (paginate), keep version >= 1.2.5
print("\n=== sync_logs (version >= 1.2.5) ===")
rows = []
start = 0
page = 1000
while start < 15000:
    batch = (
        sb.table("sync_logs")
        .select(
            "machine_name,company_name,status,error_details,app_version,"
            "updated_at,started_at,finished_at,steps_failed"
        )
        .not_.is_("app_version", "null")
        .order("updated_at", desc=True)
        .range(start, start + page - 1)
        .execute()
        .data
        or []
    )
    if not batch:
        break
    for x in batch:
        vt = ver_tuple(x.get("app_version"))
        if vt and vt >= MIN_VER:
            rows.append(x)
    if len(batch) < page:
        break
    start += page

print("matched_rows=", len(rows))
print("BY_VERSION=", dict(Counter(x.get("app_version") for x in rows)))
print("BY_STATUS=", dict(Counter(x.get("status") for x in rows)))

# last 7 days subset
week = []
for x in rows:
    dt = pdt(x.get("updated_at"))
    if dt and dt >= now - timedelta(days=7):
        week.append(x)
print("\nLAST_7D rows=", len(week))
print("LAST_7D BY_VERSION=", dict(Counter(x.get("app_version") for x in week)))
print("LAST_7D BY_STATUS=", dict(Counter(x.get("status") for x in week)))

failed = [x for x in week if str(x.get("status") or "").lower() in ("failed", "error", "partial")]
completed = [x for x in week if str(x.get("status") or "").lower() == "completed"]
print("LAST_7D completed=", len(completed), "failed/partial=", len(failed))

err = Counter()
for x in failed:
    msg = str(x.get("error_details") or x.get("steps_failed") or "")[:160].replace("\n", " ")
    err[(x.get("app_version"), msg)] += 1
print("\nTOP failed/partial errors (7d, by version):")
for (ver, msg), c in err.most_common(15):
    print(f"  [{c}] v={ver} | {msg}")

print("\nLATEST 25 versioned>=1.2.5 logs:")
for x in rows[:25]:
    em = str(x.get("error_details") or "")[:100].replace("\n", " ")
    print(
        x.get("updated_at"),
        "|",
        str(x.get("status")).ljust(10),
        "| v=",
        x.get("app_version"),
        "|",
        x.get("machine_name"),
        "|",
        x.get("company_name"),
        "|",
        em,
    )

# Companies with completed in 7d
ok_cos = sorted(
    {
        x.get("company_name")
        for x in completed
        if x.get("company_name")
    }
)
print("\nCompanies COMPLETED in 7d (version>=1.2.5):", len(ok_cos))
for c in ok_cos:
    print(" ", c)

# script_control subscriptions
print("\n=== script_control / subscriptions ===")
sc = (
    sb.table("script_control")
    .select(
        "machine_name,sync_should_run,expires_at,app_version,last_seen_at,current_company"
    )
    .execute()
    .data
    or []
)
print("machines=", len(sc))
by_run = Counter(bool(x.get("sync_should_run")) for x in sc)
print("should_run true/false=", dict(by_run))
by_ver = Counter(x.get("app_version") or "(null)" for x in sc)
print("ctrl BY_VERSION=", dict(by_ver))

live = []
expired = []
ok_lic = []
no_exp = []
for r in sc:
    exp = pdt(r.get("expires_at"))
    last = pdt(r.get("last_seen_at"))
    age = None if not last else round((now - last).total_seconds() / 3600, 1)
    if r.get("expires_at") in (None, ""):
        lic = "NO_EXPIRY"
        no_exp.append(r)
    elif exp and exp < now:
        lic = "EXPIRED"
        expired.append(r)
    else:
        lic = "OK"
        ok_lic.append(r)
    if last and (now - last).total_seconds() < 6 * 3600:
        live.append((r, lic, age))

print("licence OK=", len(ok_lic), "EXPIRED=", len(expired), "NO_EXPIRY=", len(no_exp))
print("\nLIVE <6h:")
for r, lic, age in sorted(live, key=lambda x: x[2] or 999):
    print(
        r.get("machine_name"),
        "| should_run=",
        r.get("sync_should_run"),
        "| v=",
        r.get("app_version"),
        "|",
        lic,
        "| age_h=",
        age,
        "|",
        r.get("current_company"),
        "| exp=",
        r.get("expires_at"),
    )

print("\nEXPIRED (paused expected):")
for r in sorted(expired, key=lambda x: x.get("machine_name") or ""):
    last = pdt(r.get("last_seen_at"))
    age = None if not last else round((now - last).total_seconds() / 3600, 1)
    print(
        r.get("machine_name"),
        "| should_run=",
        r.get("sync_should_run"),
        "| v=",
        r.get("app_version"),
        "| exp=",
        r.get("expires_at"),
        "| age_h=",
        age,
        "|",
        r.get("current_company"),
    )

print("\nENABLED (should_run=true) with OK licence but offline >24h:")
for r in ok_lic:
    if r.get("sync_should_run") is not True:
        continue
    last = pdt(r.get("last_seen_at"))
    if last and (now - last).total_seconds() < 24 * 3600:
        continue
    age = None if not last else round((now - last).total_seconds() / 3600, 1)
    print(
        r.get("machine_name"),
        "| v=",
        r.get("app_version"),
        "| exp=",
        r.get("expires_at"),
        "| age_h=",
        age,
        "|",
        r.get("current_company"),
    )

print("\nENABLED with NO_EXPIRY (odd):")
for r in no_exp:
    if r.get("sync_should_run") is not True:
        continue
    last = pdt(r.get("last_seen_at"))
    age = None if not last else round((now - last).total_seconds() / 3600, 1)
    print(
        r.get("machine_name"),
        "| v=",
        r.get("app_version"),
        "| age_h=",
        age,
        "|",
        r.get("current_company"),
    )

# Who is on < 1.2.8 among recently active
print("\n=== version adoption (last_seen < 72h) ===")
for r in sorted(sc, key=lambda x: x.get("last_seen_at") or "", reverse=True):
    last = pdt(r.get("last_seen_at"))
    if not last or (now - last).total_seconds() > 72 * 3600:
        continue
    age = round((now - last).total_seconds() / 3600, 1)
    print(
        r.get("machine_name"),
        "| v=",
        r.get("app_version"),
        "| should_run=",
        r.get("sync_should_run"),
        "| age_h=",
        age,
        "|",
        r.get("current_company"),
    )
