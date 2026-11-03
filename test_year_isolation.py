"""Year-company isolation: date-scoped voucher delete + report upsert key."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

from datetime import date

from src.date_utils import (
    company_period_bounds,
    company_period_start,
    get_company_item_fetch_window,
    get_company_voucher_date_ranges,
    indian_fy_end_for,
    iso_window_from_tally_ranges,
    tally_ymd_to_iso,
)
from src import local_db


class YearIsolationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "sync_cache.db")
        self._path_patch = patch.object(local_db, "get_db_path", return_value=self.db_path)
        self._path_patch.start()
        local_db.init_db()

    def tearDown(self):
        self._path_patch.stop()
        self.tmp.cleanup()

    def _count(self, table, company):
        conn = local_db.get_connection()
        try:
            return conn.execute(
                f'SELECT COUNT(*) FROM "{table}" WHERE company_name = ?',
                (company,),
            ).fetchone()[0]
        finally:
            conn.close()

    def test_books_from_window_covers_that_company_year(self):
        ranges = get_company_voucher_date_ranges("2023-04-01")
        self.assertTrue(ranges)
        self.assertEqual(ranges[0][0], "20230401")
        self.assertLess(ranges[0][0], "20260401")
        self.assertGreaterEqual(ranges[-1][1], "20261101")
        start, end = iso_window_from_tally_ranges(ranges)
        self.assertEqual(start, "2023-04-01")
        item = get_company_item_fetch_window("2023-04-01")
        self.assertEqual(item, [(ranges[0][0], ranges[-1][1])])

    def test_missing_books_from_falls_back_to_current_fy(self):
        ranges = get_company_voucher_date_ranges(None)
        self.assertTrue(ranges)
        self.assertEqual(ranges[0][0], "20260401")

    def test_sync_start_year_override_pins_one_fy(self):
        with patch.dict(os.environ, {"SYNC_START_YEAR": "2024"}, clear=False):
            ranges = get_company_voucher_date_ranges("2023-04-01")
        self.assertEqual(ranges[0][0], "20240401")
        self.assertEqual(ranges[-1][1], "20250331")

    def test_report_period_follows_books_from(self):
        self.assertEqual(company_period_start(books_from_iso="2023-04-01").isoformat(), "2023-04-01")
        self.assertEqual(company_period_start(books_from_iso=None).isoformat(), "2026-04-01")

    def test_report_bounds_cap_past_fy_at_year_end(self):
        start, end = company_period_bounds(
            as_on=date(2026, 11, 3),
            books_from_iso="2024-04-01",
        )
        self.assertEqual(start.isoformat(), "2024-04-01")
        self.assertEqual(end.isoformat(), "2025-03-31")
        self.assertEqual(indian_fy_end_for(date(2024, 4, 1)).isoformat(), "2025-03-31")

    def test_report_bounds_open_fy_uses_today(self):
        start, end = company_period_bounds(
            as_on=date(2026, 11, 3),
            books_from_iso="2026-04-01",
        )
        self.assertEqual(start.isoformat(), "2026-04-01")
        self.assertEqual(end.isoformat(), "2026-11-03")

    def test_report_bounds_without_books_from_use_current_fy(self):
        start, end = company_period_bounds(as_on=date(2026, 11, 3), books_from_iso=None)
        self.assertEqual(start.isoformat(), "2026-04-01")
        self.assertEqual(end.isoformat(), "2026-11-03")

    def test_tally_window_iso(self):
        self.assertEqual(tally_ymd_to_iso("20260401"), "2026-04-01")
        self.assertEqual(tally_ymd_to_iso("2026-11-30"), "2026-11-30")
        self.assertEqual(
            iso_window_from_tally_ranges([("20260401", "20260430"), ("20261101", "20261130")]),
            ("2026-04-01", "2026-11-30"),
        )

    def test_sales_reconcile_keeps_older_year(self):
        company = "S & D GlobeMate  (2026-2027)"
        local_db.save_to_local_db(
            "sales_invoices",
            [
                {
                    "guid": "sale-2024",
                    "invoice_number": "S24",
                    "invoice_date": "2024-06-15",
                    "total_amount": 100,
                },
                {
                    "guid": "sale-2026-keep",
                    "invoice_number": "S26A",
                    "invoice_date": "2026-05-01",
                    "total_amount": 200,
                },
                {
                    "guid": "sale-2026-gone",
                    "invoice_number": "S26B",
                    "invoice_date": "2026-06-01",
                    "total_amount": 50,
                },
            ],
            company,
        )
        deleted = local_db.reconcile_local_deletions(
            "sales_invoices",
            {"sale-2026-keep"},
            company,
            date_from_iso="2026-04-01",
            date_to_iso="2026-11-30",
        )
        self.assertEqual(deleted, 1)
        conn = local_db.get_connection()
        try:
            guids = {
                r["guid"]
                for r in conn.execute(
                    'SELECT guid FROM sales_invoices WHERE company_name = ?',
                    (company,),
                )
            }
        finally:
            conn.close()
        self.assertIn("sale-2024", guids)
        self.assertIn("sale-2026-keep", guids)
        self.assertNotIn("sale-2026-gone", guids)

    def test_sales_reconcile_without_window_would_wipe_old_year(self):
        company = "S & D GlobeMate  (2026-2027)"
        local_db.save_to_local_db(
            "sales_invoices",
            [
                {"guid": "sale-2024", "invoice_date": "2024-06-15", "invoice_number": "S24"},
                {"guid": "sale-2026", "invoice_date": "2026-05-01", "invoice_number": "S26"},
            ],
            company,
        )
        deleted = local_db.reconcile_local_deletions(
            "sales_invoices",
            {"sale-2026"},
            company,
        )
        self.assertEqual(deleted, 1)
        conn = local_db.get_connection()
        try:
            guids = {
                r["guid"]
                for r in conn.execute(
                    'SELECT guid FROM sales_invoices WHERE company_name = ?',
                    (company,),
                )
            }
        finally:
            conn.close()
        self.assertEqual(guids, {"sale-2026"})

    def test_daybook_keeps_old_year_and_deletes_only_window_parent(self):
        company = "S & D GlobeMate  (2026-2027)"
        local_db.save_to_local_db(
            "tally_daybook",
            [
                {"guid": "db-2024", "date": "2024-06-15", "voucher_number": "D24"},
                {"guid": "db-2026#sub#1", "date": "2026-05-10", "voucher_number": "D26"},
                {"guid": "db-2026", "date": "2026-05-10", "voucher_number": "D26"},
                {"guid": "db-gone", "date": "2026-07-01", "voucher_number": "DX"},
            ],
            company,
        )
        deleted = local_db.reconcile_local_deletions(
            "tally_daybook",
            {"db-2026"},
            company,
            date_from_iso="2026-04-01",
            date_to_iso="2026-11-30",
        )
        self.assertEqual(deleted, 1)
        conn = local_db.get_connection()
        try:
            guids = {
                r["guid"]
                for r in conn.execute(
                    'SELECT guid FROM tally_daybook WHERE company_name = ?',
                    (company,),
                )
            }
        finally:
            conn.close()
        self.assertIn("db-2024", guids)
        self.assertIn("db-2026", guids)
        self.assertIn("db-2026#sub#1", guids)
        self.assertNotIn("db-gone", guids)

    def test_purchase_window_same_as_sales(self):
        company = "S & D GlobeMate  (2026-2027)"
        local_db.save_to_local_db(
            "purchase_invoices",
            [
                {"guid": "p-2024", "invoice_date": "2024-08-01", "invoice_number": "P24"},
                {"guid": "p-2026-gone", "invoice_date": "2026-08-01", "invoice_number": "P26"},
            ],
            company,
        )
        deleted = local_db.reconcile_local_deletions(
            "purchase_invoices",
            {"p-other"},
            company,
            date_from_iso="2026-04-01",
            date_to_iso="2026-11-30",
        )
        self.assertEqual(deleted, 1)
        self.assertEqual(self._count("purchase_invoices", company), 1)

    def test_report_snapshot_conflict_is_name_and_guid(self):
        self.assertEqual(local_db.REPORT_SNAPSHOT_CONFLICT, "company_name,company_guid")
        self.assertEqual(
            local_db.COMPANY_MASTER_CONFLICT, "guid,company_name,company_number"
        )

    def test_empty_tally_guid_set_does_not_wipe(self):
        company = "S & D GlobeMate  (2026-2027)"
        local_db.save_to_local_db(
            "sales_invoices",
            [{"guid": "sale-2026", "invoice_date": "2026-05-01", "invoice_number": "S26"}],
            company,
        )
        deleted = local_db.reconcile_local_deletions(
            "sales_invoices",
            set(),
            company,
            date_from_iso="2026-04-01",
            date_to_iso="2026-11-30",
        )
        self.assertEqual(deleted, 0)
        self.assertEqual(self._count("sales_invoices", company), 1)


if __name__ == "__main__":
    unittest.main()
