import time
import logging
import os
import socket
import sys
from datetime import datetime, timezone
from dotenv import load_dotenv

# Windows service / cp1252 consoles crash on non-ASCII status glyphs. Force UTF-8 replace.
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
for _stream in (sys.stdout, sys.stderr):
    try:
        if hasattr(_stream, "reconfigure"):
            _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


class _SafeStreamHandler(logging.StreamHandler):
    """Never abort the sync cycle because the console can't encode a character."""

    def emit(self, record):
        try:
            msg = self.format(record)
            stream = self.stream
            encoding = getattr(stream, "encoding", None) or "utf-8"
            try:
                stream.write(msg + self.terminator)
            except UnicodeEncodeError:
                stream.write(msg.encode(encoding, errors="replace").decode(encoding, errors="replace") + self.terminator)
            self.flush()
        except Exception:
            self.handleError(record)


from src.error_format import format_error
from src.paths import get_env_path, get_sync_log_path
from src.version import APP_VERSION

# Load .env FIRST - before any src modules so SYNC_START_YEAR etc. are visible
_env_path = get_env_path()
if os.path.isfile(_env_path):
    load_dotenv(_env_path, override=True)
else:
    load_dotenv(override=True)

from src.config import load_config

import src.tally_companies as tally_companies
import src.ledgers as ledgers
import src.stock_items as stock_items
import src.sale_bill as sale_bill
import src.sale_bill_item as sale_bill_item
import src.purchase_bill as purchase_bill
import src.purchase_bill_item as purchase_bill_item
import src.outstanding_receivables as outstanding_receivables
import src.outstanding_payables as outstanding_payables
import src.invoice_status as invoice_status
import src.daybook as daybook
import src.godowns as godowns
import src.cost_centers as cost_centers
import src.settled_bills as settled_bills
import src.financial_reports as financial_reports
from src.tally_client import TallyClient

# Log next to the EXE (same folder as .env)
log_file = get_sync_log_path()

logging.basicConfig(
    level=logging.INFO, 
    format='[%(asctime)s] %(message)s', 
    datefmt='%b %d %I:%M %p', 
    handlers=[
        logging.FileHandler(log_file, encoding='utf-8'),
        _SafeStreamHandler(sys.stdout)
    ],
    force=True,
)

# Silence noisy third-party network logs
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)

sync_in_progress = False

# --- NEW SCRIPT CONTROL VARIABLES ---
last_known_sync_state = True
machine_hostname = socket.gethostname()

def check_script_control(company_name: str) -> bool:
    """
    Pings the script_control table in Supabase.
    Registers the machine if it doesn't exist, updates heartbeat/company if it does.
    Enforces remote pause (sync_should_run) and subscription validity (expires_at).
    Returns True if sync should proceed, False if paused remotely or expired.
    """
    global last_known_sync_state
    try:
        from src.config import load_config
        from supabase import create_client
        from datetime import timedelta
        
        config = load_config()
        supabase = create_client(config["supabase_url"], config["supabase_key"])
        
        now_dt = datetime.now(timezone.utc)
        now_utc = now_dt.isoformat()
        
        # 1. Fetch current row
        response = supabase.table("script_control").select("*").eq("machine_name", machine_hostname).execute()
        
        if not response.data:
            # 2. Doesn't exist, insert it (startup registration)
            validity_str = os.environ.get("SYNC_VALIDITY", "14d").strip().lower()
            days = 14
            try:
                if validity_str.endswith("m"):
                    days = int(validity_str[:-1]) * 30
                elif validity_str.endswith("y"):
                    days = int(validity_str[:-1]) * 365
                elif validity_str.endswith("d"):
                    days = int(validity_str[:-1])
                else:
                    days = int(validity_str)
            except Exception:
                days = 14
            
            default_expiry = (now_dt + timedelta(days=days)).isoformat()
            
            payload = {
                "machine_name": machine_hostname,
                "sync_should_run": True,
                "current_company": company_name,
                "last_seen_at": now_utc,
                "app_version": APP_VERSION,
            }
            # Try inserting with expires_at; gracefully fallback if column not yet added
            try:
                payload_with_exp = dict(payload)
                payload_with_exp["expires_at"] = default_expiry
                supabase.table("script_control").insert(payload_with_exp).execute()
            except Exception:
                try:
                    supabase.table("script_control").insert(payload).execute()
                except Exception:
                    payload.pop("app_version", None)
                    supabase.table("script_control").insert(payload).execute()
            last_known_sync_state = True
        else:
            row = response.data[0]
            # 3. Exists, update the heartbeat, company, and app version
            update_payload = {
                "current_company": company_name,
                "last_seen_at": now_utc,
                "app_version": APP_VERSION,
            }
            try:
                supabase.table("script_control").update(update_payload).eq(
                    "machine_name", machine_hostname
                ).execute()
            except Exception:
                update_payload.pop("app_version", None)
                supabase.table("script_control").update(update_payload).eq(
                    "machine_name", machine_hostname
                ).execute()
            
            # 4. Read the remote pause switch
            should_run = row.get("sync_should_run", True)
            
            # 5. Read and enforce subscription expiry date
            expires_at_raw = row.get("expires_at")
            is_expired = False
            if expires_at_raw:
                try:
                    exp_dt = datetime.fromisoformat(str(expires_at_raw).replace("Z", "+00:00"))
                    if now_dt > exp_dt:
                        is_expired = True
                        exp_fmt = exp_dt.strftime("%d-%b-%Y %I:%M %p")
                        logging.warning(f"[LICENSE EXPIRED] Subscription expired on {exp_fmt}. Sync paused for {machine_hostname}.")
                        print(f"\n[LICENSE EXPIRED] Subscription expired on {exp_fmt}. Sync is paused.\n")
                except Exception as e:
                    logging.warning(f"Error parsing expires_at ({expires_at_raw}): {e}")

            if not should_run:
                logging.info(f"[PAUSED] Sync is paused remotely via Admin Panel for {machine_hostname}.")

            last_known_sync_state = bool(should_run and not is_expired)
            
    except Exception as e:
        logging.warning(f"Could not reach Supabase for script_control. Using last known state (run={last_known_sync_state}). Error: {e}")
        
    return last_known_sync_state
# --- END NEW SCRIPT CONTROL VARIABLES ---

def run_sync_cycle():
    global sync_in_progress
    sync_in_progress = True
    
    logging.info("=====================================")
    logging.info("Starting new Tally data sync cycle...")
    
    # Clear caches
    if hasattr(sale_bill, "fetch_all_customers_cached") and hasattr(sale_bill.fetch_all_customers_cached, "cache_clear"):
        sale_bill.fetch_all_customers_cached.cache_clear()
    if hasattr(purchase_bill, "fetch_all_suppliers_cached") and hasattr(purchase_bill.fetch_all_suppliers_cached, "cache_clear"):
        purchase_bill.fetch_all_suppliers_cached.cache_clear()
        
    try:
        from src.local_db import init_db
        init_db()
    except Exception as e:
        logging.warning(f"Could not initialize local db: {e}")

    from src.sync_logger import SyncLogger
    
    try:
        try:
            all_companies = tally_companies.get_all_companies_details()
        except Exception as e:
            logging.info(f"Tally is open, but no company is selected/available. Waiting... (Error: {e})")
            return False

        if not all_companies:
            logging.info("No active companies found in Tally. Waiting...")
            return False

        logging.info(f"Found {len(all_companies)} active company/companies open in Tally.")

        for comp_idx, company_data in enumerate(all_companies, 1):
            company_name = company_data.get("company_name", "Unknown")
            logging.info(f"\n--- [Company {comp_idx}/{len(all_companies)}] Starting Sync for: {company_name} ---")
            
            # Initialize logger for this company (14 steps; includes financial reports)
            logger = SyncLogger(total_steps=14)
            logger.start()
            logger.set_company_name(company_name)

            # 1. Save company metadata & check remote control
            try:
                tally_companies.save_company_to_supabase(company_data)
                
                if not check_script_control(company_name):
                    logging.info(f"Sync paused remotely for {machine_hostname} on {company_name}. Skipping.")
                    logger.finish()
                    continue

                logging.info(f"[OK] Connected to Tally Company: {company_name}")
                print(f"\n [{comp_idx}/{len(all_companies)}] [1/14] Synced Company: {company_name}\n")
                logger.step_passed()
                time.sleep(0.1)
            except Exception as e:
                logger.step_failed(f"Company metadata sync failed: {format_error(e)}")
                logger.abort(format_error(e))
                continue

            # 2. Customers & Suppliers
            try:
                customers_data = ledgers.get_customers(company_name)
                count = len(customers_data) if customers_data else 0
                if customers_data:
                    ledgers.save_customers_to_supabase(customers_data, company_name)
                del_count = ledgers.reconcile_deleted_customers(company_name)
                del_msg = f" (Removed {del_count} deleted)" if del_count else ""
                logging.info(f"Synced {count} customers/suppliers{del_msg} for {company_name}")
                print(f" [2/14] Synced {count} Customers & Suppliers{del_msg}\n")
                logger.step_passed()
                time.sleep(0.1)
            except Exception as e:
                logger.step_failed(f"Customers sync failed: {format_error(e)}")

            # 3. Godowns
            try:
                godowns_data = godowns.get_godowns(company_name)
                count = len(godowns_data) if godowns_data else 0
                if godowns_data:
                    godowns.save_godowns_to_supabase(godowns_data, company_name)
                del_count = godowns.reconcile_deleted_godowns(company_name)
                del_msg = f" (Removed {del_count} deleted)" if del_count else ""
                logging.info(f"Synced {count} godowns{del_msg} for {company_name}")
                print(f" [3/14] Synced {count} Godowns{del_msg}\n")
                logger.step_passed()
                time.sleep(0.1)
            except Exception as e:
                logger.step_failed(f"Godowns sync failed: {format_error(e)}")
                
            # 4. Cost Centers
            try:
                cost_centers_data = cost_centers.get_cost_centers(company_name)
                count = len(cost_centers_data) if cost_centers_data else 0
                if cost_centers_data:
                    cost_centers.save_cost_centers_to_supabase(cost_centers_data, company_name)
                del_count = cost_centers.reconcile_deleted_cost_centers(company_name)
                del_msg = f" (Removed {del_count} deleted)" if del_count else ""
                logging.info(f"Synced {count} cost centers{del_msg} for {company_name}")
                print(f" [4/14] Synced {count} Cost Centers{del_msg}\n")
                logger.step_passed()
                time.sleep(0.1)
            except Exception as e:
                logger.step_failed(f"Cost centers sync failed: {format_error(e)}")

            # 5. Stock Items (masters + always-on balance refresh)
            try:
                items_data = stock_items.get_stock_items(company_name)
                count = len(items_data) if items_data else 0
                # Save masters when present; always refresh balances afterward
                if items_data:
                    stock_items.save_stock_items_to_supabase(items_data, company_name)
                else:
                    # Incremental master fetch may return 0 rows - still push live qty/rate
                    bal = stock_items.sync_stock_item_balances(company_name)
                    logging.info(f"[StockItems] No master changes; balance-only refresh updated {bal} row(s)")
                del_count = stock_items.reconcile_deleted_stock_items(company_name)
                del_msg = f" (Removed {del_count} deleted)" if del_count else ""
                logging.info(f"Synced {count} stock items{del_msg} for {company_name}")
                print(f" [5/14] Synced {count} Stock Items{del_msg}\n")
                logger.step_passed()
                time.sleep(0.1)
            except Exception as e:
                logging.error(f"Stock items sync failed for {company_name}: {e}")
                # Balance refresh must not be blocked by master-fetch failures
                try:
                    bal = stock_items.sync_stock_item_balances(company_name)
                    logging.info(f"[StockItems] Recovery balance refresh updated {bal} row(s) after master failure")
                except Exception as bal_err:
                    logging.warning(f"[StockItems] Recovery balance refresh also failed: {bal_err}")
                logger.step_failed(f"Stock items sync failed: {format_error(e)}")
                print(f" [5/14] ERROR Stock Items sync failed: {e}\n")
            # 6. Sale Bills
            try:
                sale_bills_data = sale_bill.get_sale_bills(company_name)
                saved_count = sale_bill.save_sale_bills_to_supabase(sale_bills_data, company_name)
                count = saved_count if saved_count > 0 else (len(sale_bills_data) if sale_bills_data else 0)
                del_count = sale_bill.reconcile_deleted_sale_bills(company_name)
                del_msg = f" (Removed {del_count} deleted)" if del_count else ""
                logging.info(f"Synced {count} sale bills{del_msg} for {company_name}")
                print(f" [6/14] Synced {count} Sale Bills{del_msg}\n")
                logger.step_passed()
                time.sleep(0.1)
            except Exception as e:
                logger.step_failed(f"Sale bills sync failed: {format_error(e)}")
                
            # 7. Sale Bill Items
            try:
                sale_bill_items_data = sale_bill_item.get_sale_bill_items(company_name)
                saved_count = sale_bill_item.save_sale_bill_items_to_supabase(sale_bill_items_data, company_name)
                count = saved_count if saved_count > 0 else (len(sale_bill_items_data) if sale_bill_items_data else 0)
                logging.info(f"Synced {count} sale bill items for {company_name}")
                print(f" [7/14] Synced {count} Sale Bill Items\n")
                logger.step_passed()
                time.sleep(0.1)
            except Exception as e:
                logger.step_failed(f"Sale bill items sync failed: {format_error(e)}")

            # 8. Purchase Bills
            try:
                purchase_bills_data = purchase_bill.get_purchase_bills(company_name)
                saved_count = purchase_bill.save_purchase_bills_to_supabase(purchase_bills_data, company_name)
                count = saved_count if saved_count > 0 else (len(purchase_bills_data) if purchase_bills_data else 0)
                del_count = purchase_bill.reconcile_deleted_purchase_bills(company_name)
                del_msg = f" (Removed {del_count} deleted)" if del_count else ""
                logging.info(f"Synced {count} purchase bills{del_msg} for {company_name}")
                print(f" [8/14] Synced {count} Purchase Bills{del_msg}\n")
                logger.step_passed()
                time.sleep(0.1)
            except Exception as e:
                logging.error(f"Purchase bills sync failed for {company_name}: {e}")
                print(f" [8/14] ERROR Purchase Bills sync failed: {e}\n")
                logger.step_failed(f"Purchase bills sync failed: {format_error(e)}")

            # 9. Purchase Bill Items
            try:
                purchase_bill_items_data = purchase_bill_item.get_purchase_bill_items(company_name)
                saved_count = purchase_bill_item.save_purchase_bill_items_to_supabase(purchase_bill_items_data, company_name)
                count = saved_count if saved_count > 0 else (len(purchase_bill_items_data) if purchase_bill_items_data else 0)
                logging.info(f"Synced {count} purchase bill items for {company_name}")
                print(f" [9/14] Synced {count} Purchase Bill Items\n")
                logger.step_passed()
                time.sleep(0.1)
            except Exception as e:
                logger.step_failed(f"Purchase bill items sync failed: {format_error(e)}")

            # 10. Outstanding Receivables (+ sales invoice Pending/Paid from bill numbers)
            try:
                outstanding_receivables_data = (
                    outstanding_receivables.get_outstanding_receivables(company_name) or []
                )
                count = len(outstanding_receivables_data)
                if outstanding_receivables_data:
                    outstanding_receivables.save_outstanding_receivables_to_supabase(
                        outstanding_receivables_data, company_name
                    )
                sales_status = invoice_status.apply_invoice_status_from_outstanding(
                    company_name,
                    "sales_invoices",
                    invoice_status.outstanding_invoice_numbers(outstanding_receivables_data),
                )
                logging.info(
                    f"Synced {count} outstanding receivables for {company_name}; "
                    f"sales status updated={sales_status.get('updated', 0)} "
                    f"(pending={sales_status.get('pending', 0)}, paid={sales_status.get('paid', 0)})"
                )
                print(
                    f" [10/14] Synced {count} Outstanding Receivables "
                    f"(sales Pending={sales_status.get('pending', 0)}, "
                    f"Paid={sales_status.get('paid', 0)})\n"
                )
                logger.step_passed()
                time.sleep(0.1)
            except Exception as e:
                logger.step_failed(f"Outstanding receivables sync failed: {format_error(e)}")
                
            # 11. Outstanding Payables (+ purchase invoice Pending/Paid from bill numbers)
            try:
                outstanding_payables_data = (
                    outstanding_payables.get_outstanding_payables(company_name) or []
                )
                count = len(outstanding_payables_data)
                if outstanding_payables_data:
                    outstanding_payables.save_outstanding_payables_to_supabase(
                        outstanding_payables_data, company_name
                    )
                purchase_status = invoice_status.apply_invoice_status_from_outstanding(
                    company_name,
                    "purchase_invoices",
                    invoice_status.outstanding_invoice_numbers(outstanding_payables_data),
                )
                logging.info(
                    f"Synced {count} outstanding payables for {company_name}; "
                    f"purchase status updated={purchase_status.get('updated', 0)} "
                    f"(pending={purchase_status.get('pending', 0)}, paid={purchase_status.get('paid', 0)})"
                )
                print(
                    f" [11/14] Synced {count} Outstanding Payables "
                    f"(purchases Pending={purchase_status.get('pending', 0)}, "
                    f"Paid={purchase_status.get('paid', 0)})\n"
                )
                logger.step_passed()
                time.sleep(0.1)
            except Exception as e:
                logger.step_failed(f"Outstanding payables sync failed: {format_error(e)}")

            # 12. Daybook
            try:
                daybook_data = daybook.get_daybook(company_name)
                saved_count = daybook.save_daybook_to_supabase(daybook_data, company_name)
                count = saved_count if saved_count > 0 else (len(daybook_data) if daybook_data else 0)
                del_count = daybook.reconcile_deleted_daybook(company_name)
                del_msg = f" (Removed {del_count} deleted)" if del_count else ""
                logging.info(f"Synced {count} daybook entries{del_msg} for {company_name}")
                print(f" [12/14] Synced {count} Daybook Entries{del_msg}\n")
                logger.step_passed()
                time.sleep(0.1)
            except Exception as e:
                logger.step_failed(f"Daybook sync failed: {format_error(e)}")

            # 13. Settled Bills
            try:
                print(f" [13/14] Syncing Settled Bills for {company_name}...\n")
                count = settled_bills.sync_settled_bills(company_name)
                count = count if count is not None else 0
                logging.info(f"Synced {count} settled bills for {company_name}")
                print(f" [13/14] Synced {count} Settled Bills\n")
                logger.step_passed()
                time.sleep(0.1)
            except Exception as e:
                logger.step_failed(f"Settled bills sync failed: {format_error(e)}")

            logging.info(f"Completed Phase 1 local extraction for company: {company_name}")

            # ===================================================================
            # PHASE 2: TALLY IS 100% FREED UP & IDLE
            # ===================================================================
            # Keep status messages ASCII-safe: WinSW/cp1252 must not abort Phase 3.
            logging.info("=" * 65)
            logging.info(
                f"[OK] PHASE 1 COMPLETE: Core modules staged for {company_name} "
                f"(financial reports run after cloud upload, when due)"
            )
            logging.info("[OK] TALLY IS NOW IDLE (Zero Tally load).")
            logging.info("=" * 65)
            print(f"\n{'=' * 65}")
            print(f"[OK] PHASE 1 COMPLETE: Core modules staged for {company_name}")
            print("[OK] TALLY IS NOW IDLE.")
            print(f"{'=' * 65}\n")

            # ===================================================================
            # PHASE 3: BACKGROUND PUSH (Local SQLite -> Supabase Cloud)
            # ===================================================================
            logger.mark_uploading()
            logging.info(f"[Phase 3] Starting upload to Supabase for {company_name}...")
            print(f"[Phase 3/3] Uploading pending records to Supabase Cloud for {company_name}...\n")
            upload_failed = False
            upload_error = ""
            try:
                from src.config import load_config
                from supabase import create_client
                from src.local_db import push_all_pending_to_supabase, get_table_counts

                config = load_config()
                supabase_client = create_client(config["supabase_url"], config["supabase_key"])
                
                push_results, push_errors = push_all_pending_to_supabase(
                    supabase_client, company_name=company_name
                )
                
                logging.info(f"[Phase 3] Supabase Cloud Sync completed for {company_name}:")
                for tbl, cnt in push_results.items():
                    if cnt > 0:
                        logging.info(f"   - {tbl}: {cnt} records pushed to Supabase")
                        print(f"   - {tbl}: {cnt} records pushed to Supabase")

                for err in push_errors:
                    logger.note_error(f"Phase 3: {err}")
                
                # Check remaining pending counts
                counts = get_table_counts(company_name=company_name)
                total_pending = sum(c.get("pending", 0) for c in counts.values())
                if total_pending == 0 and not push_errors:
                    logging.info(f"[OK] All local records are in sync with Supabase for {company_name}!")
                    print(f"\n[OK] All local records are in sync with Supabase for {company_name}!\n")
                else:
                    upload_failed = True
                    # Detailed table/row errors already in error_details via note_error
                    if total_pending:
                        upload_error = (
                            f"Phase 3 incomplete: {total_pending} records remain pending "
                            f"in SQLite (will retry next cycle)"
                        )
                    elif push_errors:
                        upload_error = f"Phase 3 upload errors on {len(push_errors)} table(s)/batch(es)"
                    logging.warning(f"[WARN] {upload_error or 'Phase 3 reported errors'}")
            except Exception as e:
                upload_failed = True
                upload_error = f"Phase 3 Supabase push failed: {format_error(e)}"
                logging.error(f"[ERROR] Phase 3 Supabase push failed for {company_name}: {e}")
                print(f"[ERROR] Supabase push failed: {e}. Data remains safely stored in local SQLite.")

            # 14. Balance Sheet & Profit and Loss — after Phase 3; twice daily
            # (first sync of day + random 2-4 PM local) so hourly cycles stay fast.
            try:
                print(f" [14/14] Financial reports (BS/P&L) for {company_name}...\n")
                fr = financial_reports.sync_financial_reports(
                    company_name=company_name,
                    company_guid=company_data.get("guid"),
                    books_from_iso=company_data.get("books_from")
                    or company_data.get("financial_year_from"),
                )
                if fr.get("skipped"):
                    reason = fr.get("reason") or "skipped"
                    logging.info(
                        f"[14/14] Financial reports skipped for {company_name} ({reason})"
                    )
                    print(f" [14/14] Financial reports skipped ({reason})\n")
                else:
                    bs_n = fr.get("bs_line_count", 0)
                    pl_n = fr.get("pl_line_count", 0)
                    logging.info(
                        f"Synced financial reports for {company_name}: "
                        f"BS={bs_n} lines, P&L={pl_n} lines"
                    )
                    print(f" [14/14] Synced Financial Reports (BS={bs_n}, P&L={pl_n})\n")
                logger.step_passed()
                time.sleep(0.1)
            except Exception as e:
                logger.step_failed(f"Financial reports sync failed: {format_error(e)}")

            logger.finish(upload_failed=upload_failed, upload_error=upload_error)


        logging.info("All company sync cycles completed successfully!")
        print("\nAll open companies finished syncing successfully!\n")
        return True

    except Exception as e:
        logging.error(f"Error during overall sync cycle: {e}")
        return False
    finally:
        sync_in_progress = False

if __name__ == "__main__":
    config = load_config()
    try:
        interval = int(config.get("sync_interval", 1800))
        if interval <= 0:
            interval = 1800
    except (ValueError, TypeError):
        interval = 1800

    interval_display = f"{interval // 60}m" if interval >= 60 else f"{interval}s"
    logging.info(
        f"Starting Tally Live Sync Service v{APP_VERSION} "
        f"(Interval: {interval}s / {interval_display})..."
    )

    while True:
        try:
            logging.info("Starting new sync cycle...")
            run_sync_cycle()
            # One-shot full reconcile: clear flag after a completed cycle
            try:
                from src.env_file import clear_full_reconcile_flag
                if clear_full_reconcile_flag():
                    logging.info("[Config] FULL_RECONCILE cleared after cycle (one-shot).")
            except Exception as e:
                logging.warning(f"Could not clear FULL_RECONCILE flag: {e}")

            # Auto-update only when idle (after sync cycle)
            try:
                from src.auto_update import check_and_apply_update
                if check_and_apply_update():
                    logging.info("[Update] Exiting so updater can replace files...")
                    sys.exit(0)
            except Exception as e:
                logging.warning(f"[Update] Skipped due to error: {e}")
        except Exception as e:
            logging.error(f"Sync cycle encountered error: {e}")
        
        # Dynamically reload config to detect if SYNC_INTERVAL was changed in .env
        try:
            config = load_config()
            interval = int(config.get("sync_interval", 1800))
            if interval <= 0:
                interval = 1800
        except Exception:
            interval = 1800

        interval_display = f"{interval // 60} minutes" if interval >= 60 else f"{interval} seconds"
        logging.info(f"Sync cycle completed. Next sync in {interval_display} ({interval}s).")
        time.sleep(interval)
