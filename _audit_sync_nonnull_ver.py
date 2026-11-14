"""Read-only: sync_logs where app_version is not null (last 7 days)."""
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


cfg = load_config()
sb = create_client(cfg["supabase_url"], cfg["supabase_key"])
since = datetime.now(timezone.utc) - timedelta(days=7)

# Fetch newest logs; filter to non-null app_version in Python (and optionally server-side)
rows_raw = []
start = 0
page = 1000
while True:
    r = (
        sb.table("sync_logs")
        .select("*")
        .not_.is_("app_version", "null")
        .order("updated_at", desc=True)
        .range(start, start + page - 1)
        .execute()
    )
    batch = r.data or []
    if not batch:
        break
    rows_raw.extend(batch)
    if len(batch) < page:
        break
    start += page
    if start > 10000:
        break

rows = []
for x in rows_raw:
    ver = x.get("app_version")
    if ver in (None, ""):
        continue
    dt = parse_dt(
        x.get("updated_at")
        or x.get("finished_at")
        or x.get("started_at")
        or x.get("synced_at")
    )
    if dt and dt >= since:
        rows.append(x)

print("WINDOW_DAYS=7")
print("FETCHED_NON_NULL_VERSION=", len(rows_raw))
print("IN_WINDOW_NON_NULL_VERSION=", len(rows))
print("BY_STATUS=", dict(Counter(x.get("status") for x in rows)))
print("BY_VERSION=", dict(Counter(x.get("app_version") for x in rows)))
print(
    "BY_MACHINE_TOP=",
    Counter(x.get("machine_name") for x in rows).most_common(15),
)
print(
    "BY_COMPANY_TOP=",
    Counter((x.get("company_name") or "(null)") for x in rows).most_common(15),
)

completed = [x for x in rows if x.get("status") == "completed"]
failed = [x for x in rows if x.get("status") == "failed"]
running = [x for x in rows if x.get("status") == "running"]

print("\nCOMPLETED_COUNT=", len(completed))
for x in completed[:30]:
    print(
        "  COMPLETED",
        x.get("updated_at"),
        x.get("machine_name"),
        x.get("company_name"),
        x.get("app_version"),
    )

print("\nRUNNING_COUNT=", len(running))
for x in running[:10]:
    print(
        "  RUNNING",
        x.get("updated_at"),
        x.get("machine_name"),
        x.get("company_name"),
        x.get("app_version"),
    )

err = Counter()
for x in failed:
    msg = (
        str(
            x.get("error_message")
            or x.get("error")
            or x.get("message")
            or x.get("details")
            or ""
        )[:180]
        .replace("\n", " ")
    )
    err[msg] += 1
print("\nFAILED_COUNT=", len(failed))
print("TOP_ERRORS:")
for msg, c in err.most_common(12):
    print(f"  [{c}] {msg}")

print("\nSTATUS_BY_VERSION:")
sv = defaultdict(Counter)
for x in rows:
    sv[x.get("app_version")][x.get("status")] += 1
for v, c in sorted(sv.items()):
    print(f"  {v}: {dict(c)}")

print("\nFAILED_BY_VERSION_MACHINE:")
fvm = Counter(
    (
        x.get("app_version"),
        x.get("machine_name"),
        x.get("company_name") or "(null)",
    )
    for x in failed
)
for k, c in fvm.most_common(20):
    print(f"  [{c}] v={k[0]} | {k[1]} | {k[2]}")

print("\nCOMPLETED_BY_VERSION_MACHINE:")
cvm = Counter(
    (
        x.get("app_version"),
        x.get("machine_name"),
        x.get("company_name") or "(null)",
    )
    for x in completed
)
for k, c in cvm.most_common(20):
    print(f"  [{c}] v={k[0]} | {k[1]} | {k[2]}")

print("\nLATEST_40:")
for x in rows[:40]:
    em = str(
        x.get("error_message")
        or x.get("error")
        or x.get("message")
        or ""
    )[:120].replace("\n", " ")
    print(
        "  "
        + str(x.get("updated_at"))
        + " | "
        + str(x.get("status")).ljust(10)
        + " | v="
        + str(x.get("app_version"))
        + " | "
        + str(x.get("machine_name"))
        + " | "
        + str(x.get("company_name"))
        + " | "
        + em
    )
