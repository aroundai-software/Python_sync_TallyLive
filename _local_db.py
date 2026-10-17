import sqlite3, json
from collections import defaultdict
from pathlib import Path

db = Path("data/sync_cache.db")
conn = sqlite3.connect(db)
conn.row_factory = sqlite3.Row
tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
print("tables with day/cash", [t for t in tables if "day" in t.lower() or "cash" in t.lower() or "customer" in t.lower()])

# daybook columns
cols = [r[1] for r in conn.execute('PRAGMA table_info("tally_daybook")').fetchall()]
print("daybook cols", cols)

# sample April rows
rows = conn.execute("""
SELECT * FROM tally_daybook
WHERE company_name LIKE '%ZAHRA%'
AND (date LIKE '2026-04%' OR date LIKE '202604%' OR voucher_date LIKE '2026-04%' OR voucher_date LIKE '202604%')
LIMIT 3
""").fetchall() if "date" in [c.lower() for c in cols] or "voucher_date" in [c.lower() for c in cols] else []
print("april sample count query...")
# discover date col
date_cols = [c for c in cols if "date" in c.lower()]
print("date cols", date_cols)
if date_cols:
    dc = date_cols[0]
    rows = conn.execute(f'''SELECT * FROM tally_daybook WHERE company_name LIKE "%ZAHRA%" AND "{dc}" LIKE "%2026-04%" LIMIT 2''').fetchall()
    print("rows", len(rows))
    if rows:
        print(dict(rows[0]))

# customers ledger_type / parent
ccols = [r[1] for r in conn.execute('PRAGMA table_info("customers")').fetchall()]
print("customer cols sample", [c for c in ccols if any(x in c.lower() for x in ("parent","group","type","name","cash","bank"))])
