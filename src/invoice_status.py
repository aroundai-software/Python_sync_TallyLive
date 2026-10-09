"""
Derive sales/purchase invoice payment status from Tally outstanding bills.

Rule (same as the app):
  invoice_number in outstanding  →  Pending
  otherwise                      →  Paid

Matching is by normalized invoice number within a company (not voucher GUID).
"""

import logging
from datetime import datetime, timezone

from .local_db import (
    TABLE_MAP,
    _exact_company_clause,
    _require_company_name,
    get_connection,
)

STATUS_PENDING = "Pending"
STATUS_PAID = "Paid"


def _norm_invoice_number(value) -> str:
    if value is None:
        return ""
    return str(value).strip().lower()


def outstanding_invoice_numbers(outstandings) -> set:
    """Build a set of normalized invoice numbers from outstanding rows."""
    nums = set()
    for row in outstandings or []:
        n = _norm_invoice_number(row.get("invoicenumber") or row.get("invoice_number"))
        if n:
            nums.add(n)
    return nums


def apply_invoice_status_from_outstanding(
    company_name: str,
    invoice_table: str,
    outstanding_numbers: set,
) -> dict:
    """
    Update local invoice status for one company from outstanding invoice numbers.

    Only rows whose status changes are marked `_sync_status = pending` so Phase 3
    re-pushes them to Supabase. Returns count summary.
    """
    company_name = _require_company_name(company_name, "invoice_status")
    invoice_table = TABLE_MAP.get(invoice_table, invoice_table)
    outstanding_numbers = outstanding_numbers or set()

    summary = {"total": 0, "pending": 0, "paid": 0, "updated": 0}
    conn = get_connection()
    now = datetime.now(timezone.utc).isoformat()

    try:
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (invoice_table,),
        )
        if not cur.fetchone():
            logging.warning(
                f"[InvoiceStatus] Local table {invoice_table} missing; skip status update"
            )
            return summary

        cols = {r["name"] for r in conn.execute(f'PRAGMA table_info("{invoice_table}")')}
        if "invoice_number" not in cols or "status" not in cols:
            logging.warning(
                f"[InvoiceStatus] {invoice_table} lacks invoice_number/status; skip"
            )
            return summary

        rows = conn.execute(
            f'''
            SELECT guid, invoice_number, status
            FROM "{invoice_table}"
            WHERE {_exact_company_clause()}
            ''',
            (company_name,),
        ).fetchall()

        updates = []
        for row in rows:
            summary["total"] += 1
            inv_no = _norm_invoice_number(row["invoice_number"])
            new_status = (
                STATUS_PENDING if inv_no and inv_no in outstanding_numbers else STATUS_PAID
            )
            if new_status == STATUS_PENDING:
                summary["pending"] += 1
            else:
                summary["paid"] += 1

            old_status = (row["status"] or "").strip()
            if old_status != new_status:
                updates.append((new_status, now, company_name, row["guid"]))

        if updates:
            with conn:
                conn.executemany(
                    f'''
                    UPDATE "{invoice_table}"
                    SET status = ?,
                        updated_at = ?,
                        _sync_status = 'pending',
                        _synced_at = NULL,
                        _sync_error = NULL
                    WHERE {_exact_company_clause()} AND guid = ?
                    ''',
                    updates,
                )
            summary["updated"] = len(updates)

        logging.info(
            f"[InvoiceStatus] {invoice_table} / {company_name}: "
            f"total={summary['total']} pending={summary['pending']} "
            f"paid={summary['paid']} updated={summary['updated']} "
            f"(outstanding_keys={len(outstanding_numbers)})"
        )
    except Exception as e:
        logging.error(
            f"[InvoiceStatus] Failed updating {invoice_table} for {company_name}: {e}",
            exc_info=True,
        )
    finally:
        conn.close()

    return summary
