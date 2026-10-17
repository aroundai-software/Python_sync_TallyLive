import json
import logging
import os
import sqlite3
import sys
from datetime import datetime, timezone

# Standard mapping from internal module sync_type names to exact Supabase table names
TABLE_MAP = {
    "companies": "tally_companies",
    "tally_companies": "tally_companies",
    "customers": "customers",
    "godowns": "tally_godowns",
    "tally_godowns": "tally_godowns",
    "cost_centers": "tally_cost_centers",
    "tally_cost_centers": "tally_cost_centers",
    "stock_items": "stock_items",
    "sales": "sales_invoices",
    "sales_invoices": "sales_invoices",
    "sale_items": "invoice_items",
    "invoice_items": "invoice_items",
    "purchases": "purchase_invoices",
    "purchase_invoices": "purchase_invoices",
    "purchase_items": "invoice_items_purchase",
    "invoice_items_purchase": "invoice_items_purchase",
    "outstanding_receivables": "outstanding_receivables",
    "outstanding_payables": "outstanding_payables",
    "daybook": "tally_daybook",
    "tally_daybook": "tally_daybook",
    "settled_bills": "ledger_bill_settlements",
    "ledger_bill_settlements": "ledger_bill_settlements",
    "balance_sheet_reports": "balance_sheet_reports",
    "balance_sheet_lines": "balance_sheet_lines",
    "profit_loss_reports": "profit_loss_reports",
    "profit_loss_lines": "profit_loss_lines",
    "tally_ratio_analysis": "tally_ratio_analysis",
    "ratio_analysis": "tally_ratio_analysis",
    "tally_cash_flow": "tally_cash_flow",
}

ALL_TABLES = [
    "tally_companies",
    "customers",
    "tally_godowns",
    "tally_cost_centers",
    "stock_items",
    "sales_invoices",
    "invoice_items",
    "purchase_invoices",
    "invoice_items_purchase",
    "outstanding_receivables",
    "outstanding_payables",
    "tally_daybook",
    "ledger_bill_settlements",
    "balance_sheet_reports",
    "balance_sheet_lines",
    "profit_loss_reports",
    "profit_loss_lines",
    "tally_ratio_analysis",
    "tally_cash_flow",
]

# Full-replace snapshots: wipe company rows before staging a new extract.
SNAPSHOT_TABLES = {
    "outstanding_receivables",
    "outstanding_payables",
    "balance_sheet_reports",
    "balance_sheet_lines",
    "profit_loss_reports",
    "profit_loss_lines",
    "tally_ratio_analysis",
    "tally_cash_flow",
}

# Parent report + child lines (pushed together; lines table skipped in generic loop).
FINANCIAL_REPORT_PAIRS = (
    ("balance_sheet_reports", "balance_sheet_lines"),
    ("profit_loss_reports", "profit_loss_lines"),
)
FINANCIAL_REPORT_LINE_TABLES = {pair[1] for pair in FINANCIAL_REPORT_PAIRS}
FINANCIAL_REPORT_HEADER_TABLES = {pair[0] for pair in FINANCIAL_REPORT_PAIRS}

INTERNAL_COLS = {"_sync_status", "_synced_at", "_sync_error"}

# All business sync tables are isolated by (company_name, guid).
# tally_companies also stores company_name; local PK is composite for consistency.
# Supabase upsert for tally_companies still conflicts on guid alone (company master).
COMPOSITE_CONFLICT = "company_name,guid"
COMPANY_MASTER_CONFLICT = "guid"


def _require_company_name(company_name: str, context: str = "") -> str:
    name = (company_name or "").strip()
    if not name:
        raise ValueError(f"company_name is required{(' for ' + context) if context else ''}")
    return name


def _exact_company_clause(column: str = "company_name") -> str:
    """Strict company match only — never treat blank/NULL as shared."""
    return f'{column} = ?'


def get_db_path() -> str:
    """Returns the absolute path to sync_cache.db."""
    if getattr(sys, "frozen", False):
        base_dir = os.path.dirname(sys.executable)
    else:
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    
    data_dir = os.path.join(base_dir, "data")
    os.makedirs(data_dir, exist_ok=True)
    return os.path.join(data_dir, "sync_cache.db")


def get_connection() -> sqlite3.Connection:
    """Creates a connection with row factory and WAL mode for maximum speed and concurrency."""
    db_path = get_db_path()
    conn = sqlite3.connect(db_path, timeout=60.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA busy_timeout=60000;")
    return conn


def _table_has_composite_company_guid_pk(conn: sqlite3.Connection, table_name: str) -> bool:
    """True when PK is (company_name, guid) in any order."""
    cols = conn.execute(f'PRAGMA table_info("{table_name}")').fetchall()
    pk_cols = [r["name"].lower() for r in sorted(cols, key=lambda x: x["pk"]) if r["pk"]]
    return set(pk_cols) == {"company_name", "guid"} and len(pk_cols) == 2


def _migrate_table_to_composite_pk(conn: sqlite3.Connection, table_name: str):
    """Rebuild a legacy guid-only PK table into PRIMARY KEY (company_name, guid)."""
    exists = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    ).fetchone()
    if not exists:
        return
    if _table_has_composite_company_guid_pk(conn, table_name):
        return

    logging.info(f"[LocalDB] Migrating {table_name} to PRIMARY KEY (company_name, guid)...")
    cols = conn.execute(f'PRAGMA table_info("{table_name}")').fetchall()
    col_names = [r["name"] for r in cols]
    if "company_name" not in [c.lower() for c in col_names]:
        conn.execute(f'ALTER TABLE "{table_name}" ADD COLUMN company_name TEXT')
        cols = conn.execute(f'PRAGMA table_info("{table_name}")').fetchall()
        col_names = [r["name"] for r in cols]

    # Drop rows that cannot be isolated
    conn.execute(
        f'DELETE FROM "{table_name}" WHERE company_name IS NULL OR trim(company_name) = "" '
        f'OR guid IS NULL OR trim(guid) = ""'
    )

    tmp = f"{table_name}__cmpk_tmp"
    conn.execute(f'DROP TABLE IF EXISTS "{tmp}"')

    # Copy schema columns as TEXT/existing affinity via CREATE AS then rebuild PK
    col_defs = []
    for r in cols:
        name = r["name"]
        if name.lower() in ("company_name", "guid"):
            continue
        col_type = r["type"] or "TEXT"
        col_defs.append(f'"{name}" {col_type}')

    create_sql = f'''
        CREATE TABLE "{tmp}" (
            company_name TEXT NOT NULL,
            guid TEXT NOT NULL,
            {", ".join(col_defs) + "," if col_defs else ""}
            PRIMARY KEY (company_name, guid)
        );
    '''
    conn.execute(create_sql)

    select_cols = ["company_name", "guid"] + [
        c for c in col_names if c.lower() not in ("company_name", "guid")
    ]
    quoted = ", ".join(f'"{c}"' for c in select_cols)
    conn.execute(
        f'INSERT OR IGNORE INTO "{tmp}" ({quoted}) SELECT {quoted} FROM "{table_name}"'
    )
    conn.execute(f'DROP TABLE "{table_name}"')
    conn.execute(f'ALTER TABLE "{tmp}" RENAME TO "{table_name}"')
    logging.info(f"[LocalDB] Migrated {table_name} to composite company_name+guid key.")


def _ensure_table_schema(conn: sqlite3.Connection, table_name: str, sample_record: dict):
    """
    Ensures the table exists with PRIMARY KEY (company_name, guid) and dynamically adds
    any new columns from the record dictionary so schema matches 100%.
    """
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS "{table_name}" (
            company_name TEXT NOT NULL,
            guid TEXT NOT NULL,
            _sync_status TEXT DEFAULT 'pending',
            _synced_at TIMESTAMP,
            _sync_error TEXT,
            PRIMARY KEY (company_name, guid)
        );
    """)
    _migrate_table_to_composite_pk(conn, table_name)

    # Get existing columns
    cur = conn.execute(f'PRAGMA table_info("{table_name}")')
    existing_cols = {row["name"].lower() for row in cur.fetchall()}

    # Add any missing columns
    for key in sample_record.keys():
        if key.lower() not in existing_cols and key not in INTERNAL_COLS:
            key_lower = key.lower()
            if "alter_id" in key_lower or key_lower in ("credit_days", "overdue_days", "days_to_clear", "days_from_due_date"):
                col_type = "INTEGER"
            elif any(sub in key_lower for sub in ("amount", "balance", "rate", "quantity", "total", "vat", "tax")):
                col_type = "REAL"
            else:
                col_type = "TEXT"
            
            try:
                conn.execute(f'ALTER TABLE "{table_name}" ADD COLUMN "{key}" {col_type};')
                existing_cols.add(key.lower())
            except Exception:
                pass

    # Create helpful indexes
    try:
        conn.execute(f'CREATE INDEX IF NOT EXISTS "idx_{table_name}_status" ON "{table_name}"(_sync_status);')
        conn.execute(f'CREATE INDEX IF NOT EXISTS "idx_{table_name}_comp" ON "{table_name}"(company_name, _sync_status);')
    except Exception:
        pass


def save_to_local_db(table_name: str, records: list, company_name: str = "") -> int:
    """
    Saves or updates records in a dedicated SQLite table matching the Supabase table name.
    Isolation key: (company_name, guid). Blank company_name is rejected.
    """
    if not records:
        return 0

    table_name = TABLE_MAP.get(table_name, table_name)
    try:
        company_name = _require_company_name(company_name or records[0].get("company_name"), table_name)
    except ValueError as e:
        logging.error(f"[LocalDB] Refusing to save {table_name}: {e}")
        return 0

    conn = get_connection()
    saved_count = 0
    now = datetime.now(timezone.utc).isoformat()

    try:
        with conn:
            # Stamp company on sample so schema sees the column
            sample = dict(records[0])
            sample["company_name"] = company_name
            _ensure_table_schema(conn, table_name, sample)

            # Snapshot tables: clear only this exact company
            if table_name in SNAPSHOT_TABLES:
                conn.execute(
                    f'DELETE FROM "{table_name}" WHERE {_exact_company_clause()}',
                    (company_name,),
                )

            keys_set = set()
            for r in records[:50]:
                keys_set.update(r.keys())
            keys_list = [
                k for k in keys_set
                if k not in INTERNAL_COLS and k.lower() not in ("guid", "company_name")
            ]

            all_cols = ["company_name", "guid"] + keys_list + ["_sync_status", "_synced_at"]
            col_names_str = ", ".join([f'"{c}"' for c in all_cols])
            placeholders_str = ", ".join(["?"] * len(all_cols))
            update_assignments = ", ".join([
                f'"{c}" = excluded."{c}"' for c in keys_list + ["_sync_status", "_synced_at"]
            ])

            sql = f"""
                INSERT INTO "{table_name}" ({col_names_str})
                VALUES ({placeholders_str})
                ON CONFLICT(company_name, guid) DO UPDATE SET {update_assignments};
            """

            rows_to_insert = []
            for r in records:
                guid = r.get("guid") or r.get("Guid") or r.get("id")
                if not guid:
                    name_key = (
                        r.get("customer_name")
                        or r.get("name")
                        or r.get("invoicenumber")
                        or r.get("invoice_number")
                        or ""
                    )
                    if name_key:
                        guid = f"{table_name}_{company_name}_{name_key}".replace(" ", "_")
                    else:
                        continue

                row_vals = [company_name, str(guid)]
                for k in keys_list:
                    val = r.get(k)
                    if isinstance(val, (dict, list)):
                        val = json.dumps(val, default=str)
                    row_vals.append(val)

                row_vals.append("pending")
                row_vals.append(now)
                rows_to_insert.append(row_vals)

            if rows_to_insert:
                conn.executemany(sql, rows_to_insert)
                saved_count = len(rows_to_insert)

    except Exception as e:
        logging.error(f"[LocalDB] Failed to save {table_name} records: {e}", exc_info=True)
    finally:
        conn.close()

    return saved_count


def get_pending_from_local(table_name: str, company_name: str = None, limit: int = 500) -> tuple:
    """
    Retrieves pending records from a dedicated SQLite table.
    Returns (records_list_of_dicts, list_of_guids).
    Internal _sync_* columns are excluded so the dictionaries are 100% clean for Supabase.
    """
    table_name = TABLE_MAP.get(table_name, table_name)
    conn = get_connection()
    records = []
    guids = []

    try:
        # Check if table exists
        cur = conn.execute(f"SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table_name,))
        if not cur.fetchone():
            return [], []

        if company_name:
            company_name = _require_company_name(company_name, f"pending:{table_name}")
            query = f"""
                SELECT * FROM "{table_name}"
                WHERE _sync_status = 'pending' AND {_exact_company_clause()}
                LIMIT ?
            """
            cursor = conn.execute(query, (company_name, limit))
        else:
            query = f"""
                SELECT * FROM "{table_name}"
                WHERE _sync_status = 'pending'
                  AND company_name IS NOT NULL AND trim(company_name) != ''
                LIMIT ?
            """
            cursor = conn.execute(query, (limit,))

        rows = cursor.fetchall()
        for row in rows:
            d = dict(row)
            # Remove internal staging columns
            for col in list(INTERNAL_COLS):
                d.pop(col, None)
            # Never upload a row without company_name
            if not (d.get("company_name") or "").strip() or not d.get("guid"):
                continue
            guids.append(d.get("guid"))
            records.append(d)

    except Exception as e:
        logging.error(f"[LocalDB] Failed to get pending {table_name} records: {e}")
    finally:
        conn.close()

    return records, guids


def mark_records_synced(table_name: str, guids: list, company_name: str = None):
    """Marks a list of GUIDs as 'synced' for one company only."""
    if not guids:
        return
    table_name = TABLE_MAP.get(table_name, table_name)
    conn = get_connection()
    now = datetime.now(timezone.utc).isoformat()
    try:
        with conn:
            chunks = [guids[i:i + 500] for i in range(0, len(guids), 500)]
            for chunk in chunks:
                placeholders = ",".join(["?"] * len(chunk))
                if company_name:
                    company_name = _require_company_name(company_name, f"synced:{table_name}")
                    conn.execute(
                        f'UPDATE "{table_name}" SET _sync_status = "synced", _synced_at = ?, _sync_error = NULL '
                        f'WHERE {_exact_company_clause()} AND guid IN ({placeholders})',
                        [now, company_name] + chunk
                    )
                else:
                    conn.execute(
                        f'UPDATE "{table_name}" SET _sync_status = "synced", _synced_at = ?, _sync_error = NULL WHERE guid IN ({placeholders})',
                        [now] + chunk
                    )
    except Exception as e:
        logging.error(f"[LocalDB] Failed to mark {table_name} records as synced: {e}")
    finally:
        conn.close()


def mark_records_failed(table_name: str, guids: list, error_msg: str, company_name: str = None):
    """Marks a list of GUIDs as 'failed' for one company only."""
    if not guids:
        return
    table_name = TABLE_MAP.get(table_name, table_name)
    conn = get_connection()
    try:
        with conn:
            chunks = [guids[i:i + 500] for i in range(0, len(guids), 500)]
            for chunk in chunks:
                placeholders = ",".join(["?"] * len(chunk))
                if company_name:
                    company_name = _require_company_name(company_name, f"failed:{table_name}")
                    conn.execute(
                        f'UPDATE "{table_name}" SET _sync_status = "failed", _sync_error = ? '
                        f'WHERE {_exact_company_clause()} AND guid IN ({placeholders})',
                        [str(error_msg)[:300], company_name] + chunk
                    )
                else:
                    conn.execute(
                        f'UPDATE "{table_name}" SET _sync_status = "failed", _sync_error = ? WHERE guid IN ({placeholders})',
                        [str(error_msg)[:300]] + chunk
                    )
    except Exception as e:
        logging.error(f"[LocalDB] Failed to mark {table_name} records as failed: {e}")
    finally:
        conn.close()


# =========================================================================
# OFFLINE DELETIONS & CANCELLATIONS STAGING
# =========================================================================

def is_valid_uuid(val) -> bool:
    """Checks if a string is a valid UUID format."""
    if not val:
        return False
    import uuid
    try:
        uuid.UUID(str(val))
        return True
    except (ValueError, AttributeError, TypeError):
        return False


def stage_deletions(table_name: str, guids: list, company_name: str = "") -> int:
    """
    Purges GUIDs from the local SQLite table and stages them in 'pending_deletions'
    so Phase 3 can delete them from Supabase in the background.
    100% offline, zero network calls to Supabase during Phase 1!
    """
    if not guids:
        return 0
    table_name = TABLE_MAP.get(table_name, table_name)
    try:
        company_name = _require_company_name(company_name, f"delete:{table_name}")
    except ValueError as e:
        logging.error(f"[LocalDB] Refusing deletions without company: {e}")
        return 0
    conn = get_connection()
    now = datetime.now(timezone.utc).isoformat()
    purged_count = 0
    try:
        with conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS pending_deletions (
                    table_name TEXT NOT NULL,
                    company_name TEXT NOT NULL,
                    guid TEXT NOT NULL,
                    created_at TIMESTAMP,
                    PRIMARY KEY (table_name, company_name, guid)
                );
            """)
            # Migrate legacy pending_deletions PK if needed
            pk_info = conn.execute("PRAGMA table_info(pending_deletions)").fetchall()
            pk_cols = [r["name"].lower() for r in sorted(pk_info, key=lambda x: x["pk"]) if r["pk"]]
            if set(pk_cols) != {"table_name", "company_name", "guid"}:
                conn.execute("ALTER TABLE pending_deletions RENAME TO pending_deletions_legacy")
                conn.execute("""
                    CREATE TABLE pending_deletions (
                        table_name TEXT NOT NULL,
                        company_name TEXT NOT NULL,
                        guid TEXT NOT NULL,
                        created_at TIMESTAMP,
                        PRIMARY KEY (table_name, company_name, guid)
                    );
                """)
                try:
                    conn.execute("""
                        INSERT OR IGNORE INTO pending_deletions (table_name, company_name, guid, created_at)
                        SELECT table_name, COALESCE(NULLIF(trim(company_name), ''), '__UNKNOWN__'), guid, created_at
                        FROM pending_deletions_legacy
                        WHERE guid IS NOT NULL
                    """)
                except Exception:
                    pass
                conn.execute("DROP TABLE pending_deletions_legacy")

            del_rows = [(table_name, company_name, str(g), now) for g in guids]
            conn.executemany(
                "INSERT OR REPLACE INTO pending_deletions (table_name, company_name, guid, created_at) VALUES (?, ?, ?, ?)",
                del_rows
            )
            chunks = [guids[i:i + 500] for i in range(0, len(guids), 500)]
            for chunk in chunks:
                placeholders = ",".join(["?"] * len(chunk))
                conn.execute(
                    f'DELETE FROM "{table_name}" WHERE {_exact_company_clause()} AND guid IN ({placeholders})',
                    [company_name] + chunk,
                )
                if table_name == "tally_daybook":
                    for g in chunk:
                        conn.execute(
                            'DELETE FROM "tally_daybook" WHERE company_name = ? AND guid LIKE ?',
                            (company_name, f"{g}#sub#%"),
                        )
            purged_count = len(guids)
    except Exception as e:
        logging.error(f"[LocalDB] Error staging deletions for {table_name}: {e}")
    finally:
        conn.close()
    return purged_count


def reconcile_local_deletions(table_name: str, tally_active_guids: set, company_name: str) -> int:
    """
    Compares tally_active_guids against records currently in local SQLite table for company_name.
    Any local guid NOT in tally_active_guids was deleted in Tally.
    Stages it for deletion in Supabase and purges it from local SQLite.
    Zero Supabase calls! 100% offline!
    """
    table_name = TABLE_MAP.get(table_name, table_name)
    conn = get_connection()
    try:
        cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table_name,))
        if not cur.fetchone():
            return 0
        
        company_name = _require_company_name(company_name, f"reconcile:{table_name}")
        rows = conn.execute(
            f'SELECT guid FROM "{table_name}" WHERE {_exact_company_clause()}',
            (company_name,),
        ).fetchall()
            
        if not tally_active_guids:
            return 0

        local_guids = {r["guid"] for r in rows if r["guid"]}
        if table_name == "tally_daybook":
            deleted_guids = [g for g in local_guids if g.split("#sub#")[0] not in tally_active_guids]
        else:
            deleted_guids = list(local_guids - tally_active_guids)
        
        if deleted_guids:
            stage_deletions(table_name, deleted_guids, company_name)
            logging.info(f"[LocalDB] Reconciled {len(deleted_guids)} deleted items from {table_name}")
            return len(deleted_guids)
        return 0
    except Exception as e:
        logging.error(f"[LocalDB] Reconcile deletions error for {table_name}: {e}")
        return 0
    finally:
        conn.close()


# =========================================================================
# LOCAL CHECKPOINT ENGINE (OFFLINE-FIRST)
# =========================================================================

def get_max_alter_id_local(table_name: str, company_name: str = "") -> int:
    """
    Checks the local SQLite table for the maximum alter_id.
    - If table does not exist or has 0 rows -> returns 0 (pulls 100% full master data from Tally).
    - If table already has data -> returns highest alter_id (pulls only new/modified items from Tally).
    - Zero network calls to Supabase!
    """
    table_name = TABLE_MAP.get(table_name, table_name)
    conn = get_connection()
    try:
        cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table_name,))
        if not cur.fetchone():
            return 0
        cur = conn.execute(f'PRAGMA table_info("{table_name}")')
        cols = {row["name"].lower() for row in cur.fetchall()}
        if "alter_id" not in cols:
            return 0
        if company_name:
            company_name = _require_company_name(company_name, f"alter_id:{table_name}")
            res = conn.execute(
                f'SELECT MAX(CAST(alter_id AS INTEGER)) FROM "{table_name}" WHERE {_exact_company_clause()} AND alter_id IS NOT NULL',
                (company_name,)
            ).fetchone()
        else:
            res = conn.execute(
                f'SELECT MAX(CAST(alter_id AS INTEGER)) FROM "{table_name}" WHERE alter_id IS NOT NULL'
            ).fetchone()
        return int(res[0]) if res and res[0] is not None else 0
    except Exception as e:
        logging.warning(f"[LocalDB] Could not get max alter_id for {table_name}: {e}")
        return 0
    finally:
        conn.close()


def get_local_guids(table_name: str, company_name: str) -> set:
    """Returns all GUIDs currently staged locally for one company."""
    table_name = TABLE_MAP.get(table_name, table_name)
    conn = get_connection()
    try:
        cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table_name,))
        if not cur.fetchone():
            return set()
        company_name = _require_company_name(company_name, f"guids:{table_name}")
        rows = conn.execute(
            f'SELECT guid FROM "{table_name}" WHERE {_exact_company_clause()} AND guid IS NOT NULL',
            (company_name,),
        ).fetchall()
        return {r["guid"] for r in rows if r["guid"]}
    except Exception as e:
        logging.warning(f"[LocalDB] Could not get local guids for {table_name}: {e}")
        return set()
    finally:
        conn.close()


def get_local_customers_map(company_name: str = "") -> dict:
    """
    Returns {customer_name.strip().lower(): {'id': guid, 'ledger_type': ledger_type}}
    directly from local SQLite 'customers' table. 100% offline, zero network delay.
    """
    conn = get_connection()
    cust_map = {}
    try:
        cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='customers'")
        if not cur.fetchone():
            return {}
        if company_name:
            company_name = _require_company_name(company_name, "customers_map")
            rows = conn.execute(
                f'SELECT * FROM "customers" WHERE {_exact_company_clause()}',
                (company_name,),
            ).fetchall()
        else:
            rows = conn.execute('SELECT * FROM "customers"').fetchall()
        for r in rows:
            name = (r["customer_name"] or "").strip().lower() if "customer_name" in r.keys() else ""
            if name:
                cust_map[name] = {
                    "id": r["guid"],
                    "ledger_type": r["ledger_type"] if "ledger_type" in r.keys() else None,
                }
    except Exception as e:
        logging.warning(f"[LocalDB] Failed to get local customer map: {e}")
    finally:
        conn.close()
    return cust_map


def get_local_invoice_map(table_name: str, company_name: str = "") -> dict:
    """
    Returns {guid: guid} directly from local 'sales_invoices' or 'purchase_invoices' table.
    Enables line items to link to vouchers locally with zero Supabase dependencies.
    """
    table_name = TABLE_MAP.get(table_name, table_name)
    conn = get_connection()
    inv_map = {}
    try:
        cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table_name,))
        if not cur.fetchone():
            return {}
        if company_name:
            company_name = _require_company_name(company_name, f"invoice_map:{table_name}")
            rows = conn.execute(
                f'SELECT guid FROM "{table_name}" WHERE {_exact_company_clause()}',
                (company_name,),
            ).fetchall()
        else:
            rows = conn.execute(f'SELECT guid FROM "{table_name}"').fetchall()
        for r in rows:
            g = r["guid"]
            if g:
                inv_map[g] = g
    except Exception as e:
        logging.warning(f"[LocalDB] Failed to get local invoice map for {table_name}: {e}")
    finally:
        conn.close()
    return inv_map


def get_local_stock_item_master_map(company_name: str = "") -> dict:
    """
    Returns {item_name.lower(): master_id} directly from local SQLite 'stock_items' table.
    """
    conn = get_connection()
    stock_map = {}
    try:
        cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='stock_items'")
        if not cur.fetchone():
            return {}
        cols = {row["name"].lower() for row in conn.execute('PRAGMA table_info("stock_items")').fetchall()}
        if "itemname" not in cols:
            return {}
        if company_name:
            company_name = _require_company_name(company_name, "stock_map")
            rows = conn.execute(
                f'SELECT "ItemName", master_id FROM "stock_items" WHERE {_exact_company_clause()}',
                (company_name,),
            ).fetchall()
        else:
            rows = conn.execute('SELECT "ItemName", master_id FROM "stock_items"').fetchall()
        for r in rows:
            name = (r["ItemName"] or "").strip().lower()
            if name:
                stock_map[name] = str(r["master_id"]) if r["master_id"] is not None else None
    except Exception as e:
        logging.warning(f"[LocalDB] Failed to get local stock item map: {e}")
    finally:
        conn.close()
    return stock_map


# =========================================================================
# TYPE COERCION: Enforce Supabase column types before upload
# =========================================================================

_INTEGER_FIELDS_BY_TABLE = {
    # All tables: alter_id is integer
    "*": {"alter_id"},
    "stock_items": {"ItemQuantity"},
    "invoice_items": {"quantity", "free_quantity"},
    "outstanding_receivables": {"overdue_days", "credit_days"},
    "outstanding_payables": {"overdue_days", "credit_days"},
    "tally_daybook": {"alter_id"},
    "ledger_bill_settlements": {"days_to_clear", "days_from_due_date", "alter_id"},
    "balance_sheet_lines": {"sort_order"},
    "profit_loss_lines": {"sort_order"},
}

# Local-only PK; Supabase report headers conflict on company_guid; lines use report_id.
_FINANCIAL_UPLOAD_STRIP = {"guid", "id", "report_id"}

_TEXT_FIELDS_BY_TABLE = {
    # master_id is TEXT on all tables
    "*": {"master_id"},
}


def _coerce_record_types(rec: dict, table_name: str) -> dict:
    """
    Enforce Supabase column types on a record dict before uploading.
    Prevents 'invalid input syntax for type integer' and similar errors.
    """
    int_fields = _INTEGER_FIELDS_BY_TABLE.get("*", set()) | _INTEGER_FIELDS_BY_TABLE.get(table_name, set())
    text_fields = _TEXT_FIELDS_BY_TABLE.get("*", set()) | _TEXT_FIELDS_BY_TABLE.get(table_name, set())

    coerced = {}
    for k, v in rec.items():
        if k in int_fields:
            try:
                coerced[k] = int(round(float(v))) if v is not None else None
            except (TypeError, ValueError):
                coerced[k] = None
        elif k in text_fields:
            coerced[k] = str(v) if v is not None else None
        else:
            coerced[k] = v
    return coerced


# =========================================================================
# CLOUD UPLOAD ENGINE (PHASE 3: SQLite -> Supabase)
# =========================================================================

def _load_parent_uuid_map(supabase, parent_table: str, company_name: str = None) -> dict:
    """Fetch {tally_guid: supabase_uuid} for sales_invoices or purchase_invoices."""
    parent_guid_to_uuid = {}
    try:
        offset = 0
        while True:
            q = supabase.table(parent_table).select("id, guid")
            if company_name:
                q = q.eq("company_name", company_name)
            res = q.range(offset, offset + 999).execute()
            rows = res.data or []
            for r in rows:
                if r.get("guid") and r.get("id"):
                    parent_guid_to_uuid[r["guid"]] = r["id"]
            if len(rows) < 1000:
                break
            offset += 1000
    except Exception as e:
        logging.warning(
            f"[LocalDB->Supabase] Could not fetch {parent_table} UUID map: {e}"
        )
    return parent_guid_to_uuid


def _item_parent_voucher_guid(rec: dict, item_guid: str, id_field: str) -> str:
    """Best-effort Tally voucher GUID for a line item row."""
    if rec.get("voucher_guid"):
        return str(rec["voucher_guid"])
    curr = rec.get(id_field)
    g = str(item_guid or "")
    # Item guids are written as "{voucher_guid}-{line_idx}"
    if "-" in g:
        return g.rsplit("-", 1)[0]
    return str(curr) if curr else g


def _resolve_parent_uuid(rec: dict, item_guid: str, id_field: str, parent_map: dict):
    """
    Return Supabase parent UUID, or None if not in map.
    Always prefers map lookup — Tally GUIDs can look like UUIDs.
    """
    curr = rec.get(id_field)
    v_guid = _item_parent_voucher_guid(rec, item_guid, id_field)
    mapped = parent_map.get(v_guid) or (parent_map.get(str(curr)) if curr else None)
    if mapped:
        return mapped
    # Already a real cloud UUID from a previous resolve / cloud-linked stage
    if curr and is_valid_uuid(curr) and curr in parent_map.values():
        return str(curr)
    return None


def _local_parent_rows_by_guids(
    parent_table: str, company_name: str, guids: list
) -> list:
    """Load parent invoice rows from SQLite by guid (any sync status)."""
    if not guids or not company_name:
        return []
    parent_table = TABLE_MAP.get(parent_table, parent_table)
    conn = get_connection()
    out = []
    try:
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (parent_table,),
        )
        if not cur.fetchone():
            return []
        for i in range(0, len(guids), 400):
            chunk = [str(g) for g in guids[i : i + 400] if g]
            if not chunk:
                continue
            placeholders = ",".join("?" * len(chunk))
            rows = conn.execute(
                f'SELECT * FROM "{parent_table}" '
                f'WHERE {_exact_company_clause()} AND guid IN ({placeholders})',
                [company_name] + chunk,
            ).fetchall()
            for row in rows:
                d = dict(row)
                for col in list(INTERNAL_COLS):
                    d.pop(col, None)
                if d.get("guid"):
                    out.append(d)
    except Exception as e:
        logging.warning(f"[LocalDB] Could not load local {parent_table} parents: {e}")
    finally:
        conn.close()
    return out


def _ensure_parents_in_supabase(
    supabase,
    parent_table: str,
    company_name: str,
    missing_guids: list,
    parent_map: dict,
) -> tuple:
    """
    Upsert missing parent invoices from local SQLite into Supabase, then refresh map.
    Returns (updated_map, error_or_None).
    """
    missing = sorted({str(g) for g in missing_guids if g and str(g) not in parent_map})
    if not missing:
        return parent_map, None

    local_rows = _local_parent_rows_by_guids(parent_table, company_name, missing)
    if not local_rows:
        logging.warning(
            f"[LocalDB->Supabase] {len(missing)} parent guid(s) missing in Supabase "
            f"and not found in local {parent_table} (sample: {missing[:3]})"
        )
        return parent_map, None

    upload_batch = []
    valid_guids = []
    for rec in local_rows:
        row = dict(rec)
        if parent_table == "sales_invoices":
            if not is_valid_uuid(row.get("customer_id")):
                row["customer_id"] = None
        elif parent_table == "purchase_invoices":
            row.pop("customer_id", None)
            if not is_valid_uuid(row.get("supplier_id")):
                row["supplier_id"] = None
        row["company_name"] = company_name
        upload_batch.append(_coerce_record_types(row, parent_table))
        valid_guids.append(row["guid"])

    logging.info(
        f"[LocalDB->Supabase] Ensuring {len(upload_batch)} missing {parent_table} "
        f"parent(s) exist in Supabase before line items..."
    )
    try:
        for i in range(0, len(upload_batch), 500):
            chunk = upload_batch[i : i + 500]
            chunk_guids = valid_guids[i : i + 500]
            supabase.table(parent_table).upsert(
                chunk, on_conflict=COMPOSITE_CONFLICT
            ).execute()
            mark_records_synced(parent_table, chunk_guids, company_name=company_name)
    except Exception as e:
        from .error_format import format_error
        err = f"{parent_table}: {format_error(e)}"
        logging.warning(f"[LocalDB->Supabase] Failed ensuring parents: {err}")
        return parent_map, err

    refreshed = _load_parent_uuid_map(supabase, parent_table, company_name)
    logging.info(
        f"[LocalDB->Supabase] Parent map for {parent_table}: "
        f"{len(parent_map)} -> {len(refreshed)} UUID(s)"
    )
    return refreshed, None


def push_local_table_to_supabase(
    table_name: str, supabase, company_name: str = None, batch_size: int = None
) -> tuple:
    """
    Pushes all pending records from a dedicated SQLite table to Supabase.
    - Resolves relational IDs (UUIDs) for child line items automatically.
    - Executes any pending deletions against Supabase.
    - Marks successfully uploaded records as 'synced' in SQLite.
    - Guarantees zero data loss: failed records stay 'pending' for next retry.
    """
    supabase_table = TABLE_MAP.get(table_name, table_name)
    # Daybook rows are wide/heavy for Postgres upserts - keep batches small.
    if batch_size is None:
        batch_size = 100 if supabase_table == "tally_daybook" else 500
    total_synced = 0
    last_error = None

    # Re-queue rows that should retry (orphan parents / statement timeouts)
    try:
        _conn = get_connection()
        with _conn:
            if supabase_table in ("invoice_items", "invoice_items_purchase"):
                if company_name:
                    _conn.execute(
                        f'UPDATE "{supabase_table}" SET _sync_status = "pending", _sync_error = NULL '
                        f'WHERE {_exact_company_clause()} AND _sync_status = "failed" '
                        f'AND _sync_error LIKE ?',
                        (company_name, "%Parent invoice UUID not found%"),
                    )
                else:
                    _conn.execute(
                        f'UPDATE "{supabase_table}" SET _sync_status = "pending", _sync_error = NULL '
                        f'WHERE _sync_status = "failed" AND _sync_error LIKE ?',
                        ("%Parent invoice UUID not found%",),
                    )
            if supabase_table == "tally_daybook":
                if company_name:
                    _conn.execute(
                        f'UPDATE "{supabase_table}" SET _sync_status = "pending", _sync_error = NULL '
                        f'WHERE {_exact_company_clause()} AND _sync_status = "failed" '
                        f'AND (_sync_error LIKE ? OR _sync_error LIKE ?)',
                        (company_name, "%57014%", "%statement timeout%"),
                    )
                else:
                    _conn.execute(
                        f'UPDATE "{supabase_table}" SET _sync_status = "pending", _sync_error = NULL '
                        f'WHERE _sync_status = "failed" '
                        f'AND (_sync_error LIKE ? OR _sync_error LIKE ?)',
                        ("%57014%", "%statement timeout%"),
                    )
            # After DB unique constraint is applied, retry prior ON CONFLICT failures
            if company_name:
                _conn.execute(
                    f'UPDATE "{supabase_table}" SET _sync_status = "pending", _sync_error = NULL '
                    f'WHERE {_exact_company_clause()} AND _sync_status = "failed" '
                    f'AND (_sync_error LIKE ? OR _sync_error LIKE ? OR _sync_error LIKE ?)',
                    (
                        company_name,
                        "%ON CONFLICT%",
                        "%42P10%",
                        "%no unique or exclusion constraint%",
                    ),
                )
            else:
                _conn.execute(
                    f'UPDATE "{supabase_table}" SET _sync_status = "pending", _sync_error = NULL '
                    f'WHERE _sync_status = "failed" '
                    f'AND (_sync_error LIKE ? OR _sync_error LIKE ? OR _sync_error LIKE ?)',
                    (
                        "%ON CONFLICT%",
                        "%42P10%",
                        "%no unique or exclusion constraint%",
                    ),
                )
        _conn.close()
    except Exception as repend_err:
        logging.warning(f"[LocalDB] Could not re-pend retryable {supabase_table}: {repend_err}")

    # 1. Process pending deletions for this table
    conn = get_connection()
    try:
        cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='pending_deletions'")
        if cur.fetchone():
            if company_name:
                del_rows = conn.execute(
                    "SELECT guid FROM pending_deletions WHERE table_name = ? AND company_name = ?",
                    (supabase_table, company_name)
                ).fetchall()
            else:
                del_rows = conn.execute(
                    "SELECT guid FROM pending_deletions WHERE table_name = ?",
                    (supabase_table,)
                ).fetchall()

            del_guids = [r["guid"] for r in del_rows]
            if del_guids:
                logging.info(f"[LocalDB->Supabase] Found {len(del_guids)} staged deletions for {supabase_table}...")
                for i in range(0, len(del_guids), 50):
                    chunk = del_guids[i:i + 50]
                    try:
                        def _scoped(q):
                            if company_name and supabase_table != "tally_companies":
                                return q.eq("company_name", company_name)
                            return q

                        if supabase_table == "sales_invoices":
                            # Delete child invoice items first (same company only)
                            res = _scoped(
                                supabase.table("sales_invoices").select("id").in_("guid", chunk)
                            ).execute()
                            inv_ids = [r["id"] for r in (res.data or []) if r.get("id")]
                            if inv_ids:
                                supabase.table("invoice_items").delete().in_("invoice_id", inv_ids).execute()
                        elif supabase_table == "purchase_invoices":
                            res = _scoped(
                                supabase.table("purchase_invoices").select("id").in_("guid", chunk)
                            ).execute()
                            pur_ids = [r["id"] for r in (res.data or []) if r.get("id")]
                            if pur_ids:
                                supabase.table("invoice_items_purchase").delete().in_("purchase_invoice_id", pur_ids).execute()

                        _scoped(
                            supabase.table(supabase_table).delete().in_("guid", chunk)
                        ).execute()
                        
                        # Remove from pending_deletions
                        with conn:
                            placeholders = ",".join(["?"] * len(chunk))
                            if company_name:
                                conn.execute(
                                    f"DELETE FROM pending_deletions WHERE table_name = ? AND company_name = ? AND guid IN ({placeholders})",
                                    [supabase_table, company_name] + chunk
                                )
                            else:
                                conn.execute(
                                    f"DELETE FROM pending_deletions WHERE table_name = ? AND guid IN ({placeholders})",
                                    [supabase_table] + chunk
                                )
                    except Exception as del_err:
                        logging.warning(f"[LocalDB->Supabase] Failed to delete chunk from {supabase_table}: {del_err}")
    except Exception as e:
        logging.warning(f"[LocalDB->Supabase] Pending deletions processing error: {e}")
    finally:
        conn.close()

    # 2. For snapshot tables (receivables, payables), clear old records in Supabase before uploading new snapshot
    if supabase_table in ("outstanding_receivables", "outstanding_payables") and company_name:
        try:
            supabase.table(supabase_table).delete().eq("company_name", company_name).execute()
        except Exception as ex:
            logging.warning(f"Could not clear previous {supabase_table} for {company_name}: {ex}")

    # 3. Pre-fetch parent UUID maps if uploading line items
    parent_guid_to_uuid = {}
    parent_table_for_items = None
    if supabase_table == "invoice_items":
        parent_table_for_items = "sales_invoices"
        parent_guid_to_uuid = _load_parent_uuid_map(
            supabase, parent_table_for_items, company_name
        )
        logging.info(
            f"[LocalDB->Supabase] Loaded {len(parent_guid_to_uuid)} sales_invoices "
            f"UUID(s) for line-item link"
        )
    elif supabase_table == "invoice_items_purchase":
        parent_table_for_items = "purchase_invoices"
        parent_guid_to_uuid = _load_parent_uuid_map(
            supabase, parent_table_for_items, company_name
        )
        logging.info(
            f"[LocalDB->Supabase] Loaded {len(parent_guid_to_uuid)} purchase_invoices "
            f"UUID(s) for line-item link"
        )

    # 4. Upload pending records in batches
    while True:
        records, guids = get_pending_from_local(supabase_table, company_name=company_name, limit=batch_size)
        if not records:
            break

        def _build_item_batch(recs, gs, parent_map):
            upload_batch = []
            valid_guids = []
            missing_parent_guids = []
            for r, g in zip(recs, gs):
                rec = dict(r)

                if supabase_table == "sales_invoices":
                    if not is_valid_uuid(rec.get("customer_id")):
                        rec["customer_id"] = None

                elif supabase_table == "purchase_invoices":
                    rec.pop("customer_id", None)
                    if not is_valid_uuid(rec.get("supplier_id")):
                        rec["supplier_id"] = None

                elif supabase_table == "invoice_items":
                    mapped_uuid = _resolve_parent_uuid(
                        rec, g, "invoice_id", parent_map
                    )
                    if not mapped_uuid:
                        missing_parent_guids.append(
                            _item_parent_voucher_guid(rec, g, "invoice_id")
                        )
                        continue
                    rec["invoice_id"] = mapped_uuid
                    rec.pop("voucher_guid", None)

                elif supabase_table == "invoice_items_purchase":
                    if not rec.get("purchase_invoice_id") and rec.get("invoice_id"):
                        rec["purchase_invoice_id"] = rec.get("invoice_id")
                    rec.pop("invoice_id", None)
                    mapped_uuid = _resolve_parent_uuid(
                        rec, g, "purchase_invoice_id", parent_map
                    )
                    if not mapped_uuid:
                        missing_parent_guids.append(
                            _item_parent_voucher_guid(rec, g, "purchase_invoice_id")
                        )
                        continue
                    rec["purchase_invoice_id"] = mapped_uuid
                    rec.pop("voucher_guid", None)

                if company_name:
                    rec["company_name"] = company_name
                if not (rec.get("company_name") or "").strip() and supabase_table != "tally_companies":
                    continue
                if supabase_table == "tally_companies" and not (rec.get("company_name") or "").strip():
                    continue

                if supabase_table == "tally_ratio_analysis":
                    # Local PK uses guid (= company_guid); cloud unique is company_guid.
                    rec.pop("guid", None)
                    rec.pop("id", None)
                    ratios = rec.get("ratios")
                    if isinstance(ratios, str):
                        try:
                            rec["ratios"] = json.loads(ratios) if ratios.strip() else {}
                        except Exception:
                            rec["ratios"] = {}
                    elif ratios is None:
                        rec["ratios"] = {}

                if supabase_table == "tally_cash_flow":
                    rec.pop("guid", None)
                    rec.pop("id", None)
                    months = rec.get("months")
                    if isinstance(months, str):
                        try:
                            rec["months"] = json.loads(months) if months.strip() else []
                        except Exception:
                            rec["months"] = []
                    elif months is None:
                        rec["months"] = []

                upload_batch.append(_coerce_record_types(rec, supabase_table))
                valid_guids.append(g)
            return upload_batch, valid_guids, missing_parent_guids

        upload_batch, valid_guids, missing_parents = _build_item_batch(
            records, guids, parent_guid_to_uuid
        )

        # If line items lack parents in cloud, push those parents from local SQLite then retry
        if (
            parent_table_for_items
            and missing_parents
            and len(upload_batch) < len(records)
        ):
            parent_guid_to_uuid, ensure_err = _ensure_parents_in_supabase(
                supabase,
                parent_table_for_items,
                company_name,
                missing_parents,
                parent_guid_to_uuid,
            )
            if ensure_err:
                last_error = ensure_err
            upload_batch, valid_guids, missing_parents = _build_item_batch(
                records, guids, parent_guid_to_uuid
            )

        if not upload_batch:
            if parent_table_for_items:
                msg = (
                    f"{len(guids)} {supabase_table} row(s) waiting for parent invoice "
                    f"UUID in Supabase (map_size={len(parent_guid_to_uuid)}, "
                    f"missing_sample={missing_parents[:3]!r}; will retry next cycle)"
                )
                logging.warning(f"[LocalDB->Supabase] {msg}")
                last_error = f"{supabase_table}: Parent invoice UUID not found in Supabase"
            else:
                logging.warning(
                    f"[LocalDB->Supabase] No uploadable rows in pending batch for {supabase_table}"
                )
                last_error = f"{supabase_table}: no uploadable rows in pending batch"
            break

        if supabase_table == "tally_companies":
            conflict_target = COMPANY_MASTER_CONFLICT
        elif supabase_table == "tally_ratio_analysis":
            conflict_target = "company_guid"
        elif supabase_table == "tally_cash_flow":
            # One snapshot per company; upsert by name so GUID truncations cannot fork rows.
            conflict_target = "company_name"
        else:
            conflict_target = COMPOSITE_CONFLICT

        try:
            supabase.table(supabase_table).upsert(
                upload_batch, on_conflict=conflict_target
            ).execute()
            mark_records_synced(supabase_table, valid_guids, company_name=company_name)
            total_synced += len(upload_batch)
            logging.info(f"[LocalDB->Supabase] Synced {total_synced} {supabase_table} to Supabase...")
        except Exception as e:
            from .error_format import format_error
            err_str = format_error(e)
            last_error = f"{supabase_table}: {err_str}"
            logging.warning(f"[LocalDB->Supabase] Batch upload error for {supabase_table}: {err_str}")
            # Graceful fallback for outstanding receivables/payables if columns missing in Supabase
            if any(k in err_str for k in ("group_name", "mobile", "PGRST204")):
                try:
                    stripped_records = [
                        {k: v for k, v in r.items() if k not in ("group_name", "mobile")}
                        for r in upload_batch
                    ]
                    supabase.table(supabase_table).upsert(
                        stripped_records, on_conflict=conflict_target
                    ).execute()
                    mark_records_synced(supabase_table, valid_guids, company_name=company_name)
                    total_synced += len(upload_batch)
                    last_error = None
                    continue
                except Exception as inner_e:
                    last_error = f"{supabase_table}: {format_error(inner_e)}"
                    mark_records_failed(
                        supabase_table, valid_guids, last_error, company_name=company_name
                    )
                    break
            elif "57014" in err_str or "statement timeout" in err_str.lower():
                # Leave pending and retry in smaller pieces (do not mark failed).
                if len(upload_batch) > 25:
                    mini = max(25, len(upload_batch) // 4)
                    logging.warning(
                        f"[LocalDB->Supabase] Retrying {supabase_table} in chunks of {mini} "
                        f"after statement timeout..."
                    )
                    timed_out = False
                    for i in range(0, len(upload_batch), mini):
                        sub = upload_batch[i : i + mini]
                        sub_guids = valid_guids[i : i + mini]
                        try:
                            supabase.table(supabase_table).upsert(
                                sub, on_conflict=conflict_target
                            ).execute()
                            mark_records_synced(
                                supabase_table, sub_guids, company_name=company_name
                            )
                            total_synced += len(sub)
                            logging.info(
                                f"[LocalDB->Supabase] Synced {total_synced} {supabase_table} "
                                f"to Supabase..."
                            )
                            last_error = None
                        except Exception as mini_e:
                            last_error = f"{supabase_table}: {format_error(mini_e)}"
                            logging.warning(
                                f"[LocalDB->Supabase] Mini-batch error for {supabase_table}: "
                                f"{last_error}"
                            )
                            timed_out = True
                            break
                    if timed_out:
                        break
                    continue
                logging.warning(
                    f"[LocalDB->Supabase] Leaving {len(valid_guids)} {supabase_table} "
                    f"row(s) pending after statement timeout (will retry next cycle)"
                )
                break
            else:
                mark_records_failed(
                    supabase_table, valid_guids, err_str, company_name=company_name
                )
                break

    return total_synced, last_error


def _load_local_financial_report(reports_table: str, company_name: str):
    """Latest local report header for company (any sync status)."""
    conn = get_connection()
    try:
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (reports_table,),
        )
        if not cur.fetchone():
            return None
        company_name = _require_company_name(company_name, f"fr:{reports_table}")
        row = conn.execute(
            f'SELECT * FROM "{reports_table}" WHERE {_exact_company_clause()} LIMIT 1',
            (company_name,),
        ).fetchone()
        if not row:
            return None
        return {k: row[k] for k in row.keys()}
    except Exception as e:
        logging.warning(f"[LocalDB] Could not load local {reports_table}: {e}")
        return None
    finally:
        conn.close()


def _load_local_financial_lines(lines_table: str, company_name: str) -> list:
    """All local line rows for company (snapshot replace on push)."""
    conn = get_connection()
    try:
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (lines_table,),
        )
        if not cur.fetchone():
            return []
        company_name = _require_company_name(company_name, f"fr:{lines_table}")
        rows = conn.execute(
            f'SELECT * FROM "{lines_table}" WHERE {_exact_company_clause()} '
            f'ORDER BY sort_order ASC',
            (company_name,),
        ).fetchall()
        return [{k: r[k] for k in r.keys()} for r in rows]
    except Exception as e:
        logging.warning(f"[LocalDB] Could not load local {lines_table}: {e}")
        return []
    finally:
        conn.close()


def _financial_report_needs_push(
    reports_table: str, lines_table: str, company_name: str
) -> bool:
    """True if header or any line is still pending/failed for this company."""
    conn = get_connection()
    try:
        company_name = _require_company_name(company_name, "fr:needs_push")
        for table_name in (reports_table, lines_table):
            cur = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (table_name,),
            )
            if not cur.fetchone():
                continue
            row = conn.execute(
                f'SELECT 1 FROM "{table_name}" WHERE {_exact_company_clause()} '
                f'AND _sync_status IN ("pending", "failed") LIMIT 1',
                (company_name,),
            ).fetchone()
            if row:
                return True
        return False
    except Exception:
        return False
    finally:
        conn.close()


def push_financial_report_snapshot(
    supabase,
    company_name: str,
    reports_table: str,
    lines_table: str,
) -> tuple:
    """
    Upsert report header (on company_guid), replace all remote lines, mark local synced.
    Same replace-snapshot semantics as the former direct Supabase write.
    """
    if not company_name:
        return 0, f"{reports_table}: company_name is required"

    if not _financial_report_needs_push(reports_table, lines_table, company_name):
        return 0, None

    report = _load_local_financial_report(reports_table, company_name)
    if not report:
        return 0, f"{reports_table}: no local snapshot for {company_name}"

    company_guid = (report.get("company_guid") or report.get("guid") or "").strip()
    if not company_guid:
        return 0, f"{reports_table}: missing company_guid for {company_name}"

    from .error_format import format_error

    report_payload = {
        k: v
        for k, v in report.items()
        if k not in INTERNAL_COLS and k not in _FINANCIAL_UPLOAD_STRIP
    }
    report_payload["company_name"] = company_name
    report_payload["company_guid"] = company_guid
    for bk in ("show_gross_profit",):
        if bk in report_payload and report_payload[bk] is not None:
            v = report_payload[bk]
            if isinstance(v, bool):
                pass
            elif isinstance(v, (int, float)):
                report_payload[bk] = bool(int(v))
            else:
                report_payload[bk] = str(v).strip().lower() in ("1", "true", "yes")
    report_payload = _coerce_record_types(report_payload, reports_table)

    try:
        upserted = (
            supabase.table(reports_table)
            .upsert(report_payload, on_conflict="company_guid")
            .execute()
        )
        report_row = (upserted.data or [None])[0]
        if not report_row or not report_row.get("id"):
            fetched = (
                supabase.table(reports_table)
                .select("id")
                .eq("company_guid", company_guid)
                .limit(1)
                .execute()
            )
            report_row = (fetched.data or [None])[0]
        if not report_row or not report_row.get("id"):
            return 0, f"{reports_table}: failed to upsert / resolve report id"
        report_id = report_row["id"]
    except Exception as e:
        err = f"{reports_table}: {format_error(e)}"
        logging.warning(f"[LocalDB->Supabase] {err}")
        mark_records_failed(
            reports_table, [report.get("guid") or company_guid], err, company_name=company_name
        )
        return 0, err

    local_lines = _load_local_financial_lines(lines_table, company_name)
    upload_lines = []
    line_guids = []
    for row in local_lines:
        guid = row.get("guid")
        rec = {
            k: v
            for k, v in row.items()
            if k not in INTERNAL_COLS and k not in _FINANCIAL_UPLOAD_STRIP
        }
        rec["report_id"] = report_id
        rec["company_name"] = company_name
        rec["company_guid"] = company_guid
        # Booleans may be stored as 0/1 in SQLite
        for bk in ("is_total", "is_calculated"):
            if bk in rec and rec[bk] is not None:
                v = rec[bk]
                if isinstance(v, bool):
                    rec[bk] = v
                elif isinstance(v, (int, float)):
                    rec[bk] = bool(int(v))
                else:
                    rec[bk] = str(v).strip().lower() in ("1", "true", "yes")
        upload_lines.append(_coerce_record_types(rec, lines_table))
        if guid:
            line_guids.append(str(guid))

    try:
        supabase.table(lines_table).delete().eq("report_id", report_id).execute()
        batch_size = 200
        for i in range(0, len(upload_lines), batch_size):
            chunk = upload_lines[i : i + batch_size]
            if chunk:
                supabase.table(lines_table).insert(chunk).execute()
    except Exception as e:
        err = f"{lines_table}: {format_error(e)}"
        logging.warning(f"[LocalDB->Supabase] {err}")
        mark_records_failed(
            reports_table, [report.get("guid") or company_guid], err, company_name=company_name
        )
        if line_guids:
            mark_records_failed(lines_table, line_guids, err, company_name=company_name)
        return 0, err

    mark_records_synced(
        reports_table, [report.get("guid") or company_guid], company_name=company_name
    )
    if line_guids:
        mark_records_synced(lines_table, line_guids, company_name=company_name)

    total = 1 + len(upload_lines)
    logging.info(
        f"[LocalDB->Supabase] Synced {reports_table}+{lines_table} for {company_name}: "
        f"1 report, {len(upload_lines)} lines"
    )
    return total, None


def push_financial_reports_for_company(supabase, company_name: str) -> tuple:
    """Push both BS and P&L snapshots for one company. Returns (results, errors)."""
    results = {}
    errors = []
    for reports_table, lines_table in FINANCIAL_REPORT_PAIRS:
        count, err = push_financial_report_snapshot(
            supabase, company_name, reports_table, lines_table
        )
        if count and count > 1:
            results[reports_table] = 1
            results[lines_table] = count - 1
        else:
            results[reports_table] = count or 0
            results[lines_table] = 0
        if err:
            errors.append(err)
    return results, errors


def push_all_pending_to_supabase(supabase, company_name: str = None) -> tuple:
    """
    Pushes all pending records across dedicated tables to Supabase.
    Follows proper relational dependency order (companies -> masters -> vouchers -> items
    -> financial report snapshots).
    Returns (results_dict, errors_list).
    """
    results = {}
    errors = []
    for table_name in ALL_TABLES:
        # Child line tables are pushed with their report headers.
        if table_name in FINANCIAL_REPORT_LINE_TABLES:
            results.setdefault(table_name, 0)
            continue
        try:
            if table_name in FINANCIAL_REPORT_HEADER_TABLES:
                if not company_name:
                    results[table_name] = 0
                    errors.append(f"{table_name}: company_name is required")
                    continue
                lines_table = next(
                    lt for ht, lt in FINANCIAL_REPORT_PAIRS if ht == table_name
                )
                count, err = push_financial_report_snapshot(
                    supabase, company_name, table_name, lines_table
                )
                if count and count > 1:
                    results[table_name] = 1
                    results[lines_table] = count - 1
                else:
                    results[table_name] = count
                    results.setdefault(lines_table, 0)
            else:
                count, err = push_local_table_to_supabase(
                    table_name, supabase, company_name=company_name
                )
                results[table_name] = count
            if err:
                errors.append(err)
        except Exception as e:
            from .error_format import format_error
            msg = f"{table_name}: {format_error(e)}"
            logging.error(f"[CloudSync] Error syncing {table_name} to Supabase: {e}")
            results[table_name] = 0
            errors.append(msg)
    # Surface a sample of row-level failures still marked failed in SQLite
    try:
        errors.extend(get_recent_upload_errors(company_name=company_name, limit=5))
    except Exception:
        pass
    return results, errors


def get_recent_upload_errors(company_name: str = None, limit: int = 5) -> list:
    """Read recent _sync_error values from local tables for sync_logs."""
    conn = get_connection()
    out = []
    try:
        for table_name in ALL_TABLES:
            supabase_table = TABLE_MAP.get(table_name, table_name)
            try:
                cols = {
                    r["name"]
                    for r in conn.execute(f'PRAGMA table_info("{supabase_table}")').fetchall()
                }
            except Exception:
                continue
            if "_sync_error" not in cols:
                continue
            if company_name and "company_name" in cols:
                rows = conn.execute(
                    f'SELECT _sync_error FROM "{supabase_table}" '
                    f'WHERE _sync_status = ? AND company_name = ? AND _sync_error IS NOT NULL '
                    f'LIMIT ?',
                    ("failed", company_name, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    f'SELECT _sync_error FROM "{supabase_table}" '
                    f'WHERE _sync_status = ? AND _sync_error IS NOT NULL LIMIT ?',
                    ("failed", limit),
                ).fetchall()
            for row in rows:
                err = (row["_sync_error"] or "").strip()
                if err:
                    line = f"{supabase_table} row failed: {err}"
                    if line not in out:
                        out.append(line)
            if len(out) >= limit:
                break
    finally:
        conn.close()
    return out[:limit]


def get_table_counts(company_name: str = None) -> dict:
    """Returns total, pending, and synced record counts across all local SQLite tables."""
    conn = get_connection()
    summary = {}
    try:
        tables_res = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()
        for t_row in tables_res:
            t = t_row["name"]
            if t == "staged_records":
                continue
            try:
                if company_name:
                    total = conn.execute(f'SELECT COUNT(*) FROM "{t}" WHERE {_exact_company_clause()}', (company_name,)).fetchone()[0]
                    pending = conn.execute(f'SELECT COUNT(*) FROM "{t}" WHERE _sync_status = "pending" AND {_exact_company_clause()}', (company_name,)).fetchone()[0]
                    synced = conn.execute(f'SELECT COUNT(*) FROM "{t}" WHERE _sync_status = "synced" AND {_exact_company_clause()}', (company_name,)).fetchone()[0]
                else:
                    total = conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
                    pending = conn.execute(f'SELECT COUNT(*) FROM "{t}" WHERE _sync_status = "pending"').fetchone()[0]
                    synced = conn.execute(f'SELECT COUNT(*) FROM "{t}" WHERE _sync_status = "synced"').fetchone()[0]
                summary[t] = {"total": total, "pending": pending, "synced": synced}
            except Exception:
                pass
    finally:
        conn.close()
    return summary


# =========================================================================
# BACKWARD COMPATIBILITY ALIASES
# =========================================================================

def stage_records(sync_type: str, records: list, company_name: str) -> int:
    """Alias for save_to_local_db to preserve compatibility with existing modules."""
    target_table = TABLE_MAP.get(sync_type, sync_type)
    return save_to_local_db(target_table, records, company_name)


def get_pending_records(sync_type: str, company_name: str = None, limit: int = 100) -> list:
    """Alias for get_pending_from_local returning [(guid, record_dict), ...] format."""
    target_table = TABLE_MAP.get(sync_type, sync_type)
    records, guids = get_pending_from_local(target_table, company_name=company_name, limit=limit)
    return list(zip(guids, records))


def mark_as_synced(guids: list, sync_type: str = None):
    """Marks guids as synced, auto-detecting the table if not explicitly passed."""
    if not guids:
        return
    if sync_type and sync_type in TABLE_MAP:
        mark_records_synced(TABLE_MAP[sync_type], guids)
        return
    
    conn = get_connection()
    try:
        sample_guid = str(guids[0])
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()]
        for t in tables:
            if conn.execute(f'SELECT 1 FROM "{t}" WHERE guid = ? LIMIT 1', (sample_guid,)).fetchone():
                mark_records_synced(t, guids)
                return
    finally:
        conn.close()


def mark_as_failed(guids: list, error_msg: str = "", sync_type: str = None):
    """Marks guids as failed, auto-detecting the table if not explicitly passed."""
    if not guids:
        return
    if sync_type and sync_type in TABLE_MAP:
        mark_records_failed(TABLE_MAP[sync_type], guids, error_msg)
        return

    conn = get_connection()
    try:
        sample_guid = str(guids[0])
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()]
        for t in tables:
            if conn.execute(f'SELECT 1 FROM "{t}" WHERE guid = ? LIMIT 1', (sample_guid,)).fetchone():
                mark_records_failed(t, guids, error_msg)
                return
    finally:
        conn.close()


def migrate_legacy_tables():
    """Migrates any old tables (godowns -> tally_godowns, cost_centers -> tally_cost_centers)."""
    conn = get_connection()
    try:
        with conn:
            # 1. Migrate 'godowns' to 'tally_godowns'
            has_old_godowns = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='godowns'").fetchone()
            if has_old_godowns:
                conn.execute('CREATE TABLE IF NOT EXISTS "tally_godowns" AS SELECT * FROM "godowns" WHERE 0;')
                try:
                    conn.execute('INSERT OR IGNORE INTO "tally_godowns" SELECT * FROM "godowns";')
                    conn.execute('DROP TABLE "godowns";')
                    logging.info("[LocalDB] Successfully migrated 'godowns' table to 'tally_godowns'.")
                except Exception as e:
                    logging.warning(f"[LocalDB] Could not migrate godowns: {e}")

            # 2. Migrate 'cost_centers' to 'tally_cost_centers'
            has_old_cc = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='cost_centers'").fetchone()
            if has_old_cc:
                conn.execute('CREATE TABLE IF NOT EXISTS "tally_cost_centers" AS SELECT * FROM "cost_centers" WHERE 0;')
                try:
                    conn.execute('INSERT OR IGNORE INTO "tally_cost_centers" SELECT * FROM "cost_centers";')
                    conn.execute('DROP TABLE "cost_centers";')
                    logging.info("[LocalDB] Successfully migrated 'cost_centers' table to 'tally_cost_centers'.")
                except Exception as e:
                    logging.warning(f"[LocalDB] Could not migrate cost_centers: {e}")

            # 3. Migrate legacy staged_records
            has_staged = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='staged_records'").fetchone()
            if has_staged:
                rows = conn.execute("SELECT sync_type, company_name, payload, status FROM staged_records").fetchall()
                if rows:
                    grouped = {}
                    for r in rows:
                        stype = r["sync_type"]
                        comp = r["company_name"]
                        try:
                            payload = json.loads(r["payload"])
                            grouped.setdefault((stype, comp), []).append(payload)
                        except Exception:
                            pass
                    for (stype, comp), items in grouped.items():
                        tbl = TABLE_MAP.get(stype, stype)
                        save_to_local_db(tbl, items, comp)
                conn.execute("DROP TABLE IF EXISTS staged_records;")
    except Exception as e:
        logging.error(f"[LocalDB] Migration error: {e}")
    finally:
        conn.close()


def init_db():
    """Initializes all 13 dedicated tables matching Supabase and migrates legacy data."""
    conn = get_connection()
    try:
        with conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS pending_deletions (
                    table_name TEXT NOT NULL,
                    company_name TEXT NOT NULL,
                    guid TEXT NOT NULL,
                    created_at TIMESTAMP,
                    PRIMARY KEY (table_name, company_name, guid)
                );
            """)
            pk_info = conn.execute("PRAGMA table_info(pending_deletions)").fetchall()
            pk_cols = [r["name"].lower() for r in sorted(pk_info, key=lambda x: x["pk"]) if r["pk"]]
            if pk_info and set(pk_cols) != {"table_name", "company_name", "guid"}:
                conn.execute("ALTER TABLE pending_deletions RENAME TO pending_deletions_legacy_init")
                conn.execute("""
                    CREATE TABLE pending_deletions (
                        table_name TEXT NOT NULL,
                        company_name TEXT NOT NULL,
                        guid TEXT NOT NULL,
                        created_at TIMESTAMP,
                        PRIMARY KEY (table_name, company_name, guid)
                    );
                """)
                try:
                    conn.execute("""
                        INSERT OR IGNORE INTO pending_deletions (table_name, company_name, guid, created_at)
                        SELECT table_name, COALESCE(NULLIF(trim(company_name), ''), '__UNKNOWN__'), guid, created_at
                        FROM pending_deletions_legacy_init WHERE guid IS NOT NULL
                    """)
                except Exception:
                    pass
                conn.execute("DROP TABLE pending_deletions_legacy_init")
            conn.execute('CREATE INDEX IF NOT EXISTS "idx_del_comp" ON pending_deletions(company_name, table_name);')

            for tbl in ALL_TABLES:
                conn.execute(f"""
                    CREATE TABLE IF NOT EXISTS "{tbl}" (
                        company_name TEXT NOT NULL,
                        guid TEXT NOT NULL,
                        _sync_status TEXT DEFAULT 'pending',
                        _synced_at TIMESTAMP,
                        _sync_error TEXT,
                        PRIMARY KEY (company_name, guid)
                    );
                """)
                _migrate_table_to_composite_pk(conn, tbl)
                conn.execute(f'CREATE INDEX IF NOT EXISTS "idx_{tbl}_status" ON "{tbl}"(_sync_status);')
                conn.execute(f'CREATE INDEX IF NOT EXISTS "idx_{tbl}_comp" ON "{tbl}"(company_name, _sync_status);')
    finally:
        conn.close()

    migrate_legacy_tables()

