import logging
import time
import requests
import xmltodict
import xml.etree.ElementTree as ET
import re
import concurrent.futures
from datetime import datetime, timezone
from xml.sax.saxutils import escape

from supabase import create_client
from .config import load_config
from .tally_companies import get_company_details


# =====================================================
# HELPERS
# =====================================================

def safe_text(parent, tag):
    if parent is None:
        return None
    node = parent.find(tag)
    if node is None:
        return None
    value = (node.text or "").strip()
    return value if value else None


def get_text(value):
    """Safely extract string from xmltodict value (handles str/dict/list)."""
    if value is None:
        return ""
    if isinstance(value, list):
        return get_text(value[0]) if value else ""
    if isinstance(value, dict):
        return str(value.get("#text", "")).strip()
    return str(value).strip()


def clean_tally_xml(xml_text):
    if not xml_text:
        return ""
    xml_text = re.sub(r'&#x[0-9A-Fa-f]+;', '', xml_text)
    xml_text = re.sub(r'[\x00-\x08\x0B-\x0C\x0E-\x1F\x7F]', '', xml_text)
    xml_text = re.sub(r"&(?!amp;|lt;|gt;|quot;|apos;)", "&amp;", xml_text)
    # Strip XML namespace prefixes (e.g. <UDF:FIELD> → <FIELD>)
    xml_text = re.sub(r'<(/?)[A-Za-z][\w]*:([\w]+)', r'<\1\2', xml_text)
    return xml_text


def parse_tally_date(date_str):
    if not date_str:
        return None
    try:
        return datetime.strptime(date_str.strip(), "%d-%b-%y").date()
    except ValueError:
        try:
            return datetime.strptime(date_str.strip(), "%d-%b-%Y").date()
        except ValueError:
            return None


def get_max_alter_id(supabase, company_name: str) -> int:
    from .local_db import get_max_alter_id_local
    return get_max_alter_id_local("ledger_bill_settlements", company_name)


def fetch_all_customers(supabase, company_name: str) -> list:
    """Fetch all customer names with original casing from local SQLite 'customers' table."""
    try:
        from .local_db import get_connection
        conn = get_connection()
        try:
            if not company_name or not str(company_name).strip():
                return []
            rows = conn.execute(
                'SELECT customer_name FROM "customers" WHERE company_name = ?',
                (company_name.strip(),)
            ).fetchall()
            return [r["customer_name"].strip() for r in rows if r["customer_name"]]
        finally:
            conn.close()
    except Exception:
        pass
    return []


# =====================================================
# FETCH LPP FOR A SINGLE LEDGER
# =====================================================

def fetch_lpp_for_ledger(tally_url: str, company_name: str, ledger_name: str) -> list:
    """
    Fetch Ledger Payment Performance report for one ledger.
    Returns list of fully-settled bill dicts (Tally calculates settlement accurately).
    """
    xml_req = f"""
    <ENVELOPE>
      <HEADER>
        <VERSION>1</VERSION>
        <TALLYREQUEST>Export</TALLYREQUEST>
        <TYPE>Data</TYPE>
        <ID>Ledger Payment Performance</ID>
      </HEADER>
      <BODY>
        <DESC>
          <STATICVARIABLES>
            <EXPLODEFLAG>Yes</EXPLODEFLAG>
            <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
            <SVCURRENTCOMPANY>{escape(company_name)}</SVCURRENTCOMPANY>
            <LEDGERNAME>{escape(ledger_name)}</LEDGERNAME>
          </STATICVARIABLES>
        </DESC>
      </BODY>
    </ENVELOPE>
    """
    local_bills = []
    try:
        resp = requests.post(
            tally_url,
            data=xml_req.encode("utf-8"),
            headers={"Content-Type": "application/xml", "Connection": "close"},
            timeout=15
        )
        if resp.status_code != 200 or not resp.text.strip():
            return []

        c_xml = clean_tally_xml(resp.text)
        r_elem = ET.fromstring(c_xml)
        c_bill = None

        for child in r_elem:
            tag = child.tag
            text = (child.text or "").strip()
            if tag == "LPPDATE":
                if c_bill and c_bill.get("cleared_date"):
                    c_bill["ledger_name"] = ledger_name
                    local_bills.append(c_bill)
                c_bill = {"bill_date": text, "bill_amount": 0.0}
            elif c_bill:
                if tag == "LPPNAME":
                    c_bill["bill_reference"] = text
                elif tag == "LPPCLEAREDDATE":
                    c_bill["cleared_date"] = text
                elif tag == "LPPDUEON":
                    c_bill["due_date"] = text
                elif tag == "BILLVCHAMOUNT" and c_bill["bill_amount"] == 0.0:
                    try:
                        c_bill["bill_amount"] = abs(float(text))
                    except ValueError:
                        pass

        if c_bill and c_bill.get("cleared_date"):
            c_bill["ledger_name"] = ledger_name
            local_bills.append(c_bill)

    except Exception:
        pass

    return local_bills


def format_bills(bills: list, company_name: str, alter_id: int, now_ts: str) -> list:
    """Format raw LPP bill dicts into Supabase-ready rows."""
    rows = []
    for bill in bills:
        b_date = parse_tally_date(bill.get("bill_date"))
        c_date = parse_tally_date(bill.get("cleared_date"))
        d_date = parse_tally_date(bill.get("due_date")) or b_date
        if not b_date or not c_date:
            continue
        ledger_name = bill.get("ledger_name", "Unknown")
        b_ref = bill.get("bill_reference", "Unknown")
        rows.append({
            "company_name": company_name,
            "ledger_name": ledger_name,
            "bill_reference": b_ref,
            "bill_date": b_date.strftime("%Y-%m-%d"),
            "bill_amount": bill.get("bill_amount", 0.0),
            "due_date": d_date.strftime("%Y-%m-%d"),
            "cleared_date": c_date.strftime("%Y-%m-%d"),
            "days_to_clear": (c_date - b_date).days,
            "days_from_due_date": (c_date - d_date).days,
            "guid": f"{company_name}-{ledger_name}-{b_ref}".replace(" ", "_"),
            "alter_id": alter_id,
            "updated_at": now_ts,
        })
    return rows


# =====================================================
# STEP 1: FIND AFFECTED LEDGERS VIA ALTERID
# =====================================================

def fetch_affected_ledgers(tally_url: str, company_name: str, max_alter_id: int) -> dict:
    """
    Fetch Receipt + Payment vouchers where alter_id > max_alter_id.
    Chunks by month during initial load to prevent Tally memory blowup and freezes.
    """
    from .date_utils import get_current_fy_monthly_date_ranges
    date_ranges = get_current_fy_monthly_date_ranges()
    if not date_ranges:
        date_ranges = [("20250401", "20260331")]

    alter_filter = f"<FILTER>AlterFilter</FILTER>" if max_alter_id > 0 else ""
    alter_formula = f"<SYSTEM TYPE=\"Formulae\" NAME=\"AlterFilter\">$AlterId &gt; {max_alter_id}</SYSTEM>" if max_alter_id > 0 else ""

    # When max_alter_id == 0, chunk by month to avoid requesting 100k vouchers at once!
    chunks_to_query = date_ranges if max_alter_id == 0 else [(date_ranges[0][0], date_ranges[-1][1])]

    affected = {}

    for c_start, c_end in chunks_to_query:
        xml_request = f"""<ENVELOPE>
    <HEADER>
        <VERSION>1</VERSION>
        <TALLYREQUEST>Export</TALLYREQUEST>
        <TYPE>Collection</TYPE>
        <ID>Payment Activity Collection</ID>
    </HEADER>
    <BODY>
        <DESC>
            <STATICVARIABLES>
                <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
                <SVCURRENTCOMPANY>{escape(company_name)}</SVCURRENTCOMPANY>
                <SVFROMDATE TYPE="Date">{c_start}</SVFROMDATE>
                <SVTODATE TYPE="Date">{c_end}</SVTODATE>
            </STATICVARIABLES>
            <TDL>
                <TDLMESSAGE>
                    <COLLECTION NAME="Payment Activity Collection">
                        <TYPE>Voucher</TYPE>
                        <FETCH>
                            GUID,
                            PARTYLEDGERNAME,
                            PARTYNAME,
                            ALTERID,
                            ISDELETED,
                            ISCANCELLED
                        </FETCH>
                        <FILTER>DateFilter</FILTER>
                        {alter_filter}
                    </COLLECTION>
                    <SYSTEM TYPE="Formulae" NAME="DateFilter">$Date &gt;= $$Date:"{c_start}" AND $Date &lt;= $$Date:"{c_end}" AND ($$IsReceipt:$VoucherTypeName OR $$IsPayment:$VoucherTypeName)</SYSTEM>
                    {alter_formula}
                </TDLMESSAGE>
            </TDL>
        </DESC>
    </BODY>
</ENVELOPE>"""

        try:
            response = requests.post(
                tally_url,
                data=xml_request.encode("utf-8"),
                headers={"Content-Type": "application/xml", "Connection": "close"},
                timeout=45
            )
            response.raise_for_status()
            xml_content = clean_tally_xml(response.text)
            data = xmltodict.parse(xml_content)

            envelope = data.get("ENVELOPE") or {} if isinstance(data, dict) else {}
            body = envelope.get("BODY") or {} if isinstance(envelope, dict) else {}
            b_data = body.get("DATA") or {} if isinstance(body, dict) else {}
            collection = b_data.get("COLLECTION") or {} if isinstance(b_data, dict) else {}

            vouchers = collection.get("VOUCHER", []) if isinstance(collection, dict) else []
            if isinstance(vouchers, dict):
                vouchers = [vouchers]

            for voucher in vouchers:
                if not isinstance(voucher, dict):
                    continue
                if get_text(voucher.get("ISDELETED")) == "Yes" or get_text(voucher.get("ISCANCELLED")) == "Yes":
                    continue
                try:
                    alter_id = int(get_text(voucher.get("ALTERID")) or 0)
                except (ValueError, TypeError):
                    alter_id = 0

                for field in ("PARTYLEDGERNAME", "PARTYNAME"):
                    ledger_name = get_text(voucher.get(field))
                    if ledger_name and (ledger_name not in affected or alter_id > affected[ledger_name]):
                        affected[ledger_name] = alter_id

        except Exception as e:
            logging.warning(f"Error fetching payment activity chunk ({c_start}-{c_end}) from Tally: {e}")

    return affected



# =====================================================
# FIRST RUN: PARALLEL FULL LOAD (all customers)
# =====================================================

def full_load_all_customers(tally_url: str, company_name: str, customers: list,
                             current_alter_id: int, now_ts: str,
                             affected_for_checkpoint: dict = None) -> list:
    """
    First-run path: fetch LPP only for customers with payment activity.
    Runs sequentially to respect Tally's single-threaded architecture.
    """
    if affected_for_checkpoint:
        target_set_lower = {k.strip().lower() for k in affected_for_checkpoint.keys()}
        target_customers = [c for c in customers if c.strip().lower() in target_set_lower]
        logging.info(
            f"First-time load: filtered {len(customers)} total ledgers to {len(target_customers)} with payment activity."
        )
    else:
        logging.info("First-time load: No ledgers with payment activity found. Skipping settled bills.")
        target_customers = []

    all_rows = []
    total_cust = len(target_customers)
    if total_cust == 0:
        return []

    for idx, ledger_name in enumerate(target_customers, 1):
        if idx % 10 == 0 or idx == total_cust:
            logging.info(f"Settled bills progress: [{idx}/{total_cust}] ledgers...")
        bills = fetch_lpp_for_ledger(tally_url, company_name, ledger_name)
        time.sleep(0.05)
        all_rows.extend(format_bills(bills, company_name, current_alter_id, now_ts))

    return all_rows


# =====================================================
# INCREMENTAL: SELECTIVE LPP (affected only)
# =====================================================

def incremental_load_affected(tally_url: str, company_name: str,
                               supabase, affected_ledgers: dict,
                               now_ts: str) -> list:
    """
    Incremental path: delete old + fetch fresh LPP only for affected ledgers.
    Runs sequentially with small pause to prevent socket contention.
    """
    logging.info(
        f"Incremental sync: processing {len(affected_ledgers)} affected ledger(s)..."
    )

    all_rows = []
    from .local_db import get_connection
    for ledger_name, receipt_alter_id in affected_ledgers.items():
        # Delete old records from local SQLite first (handles deleted receipts in Tally)
        try:
            conn = get_connection()
            with conn:
                conn.execute(
                    'DELETE FROM "ledger_bill_settlements" WHERE company_name = ? AND ledger_name = ?',
                    (company_name, ledger_name)
                )
            conn.close()
        except Exception as e:
            logging.error(f"Failed to delete old local records for {ledger_name}: {e}")

        bills = fetch_lpp_for_ledger(tally_url, company_name, ledger_name)
        time.sleep(0.05)
        all_rows.extend(format_bills(bills, company_name, receipt_alter_id, now_ts))

    return all_rows



# =====================================================
# MAIN SYNC FUNCTION
# =====================================================

def sync_settled_bills(company_name: str = None):
    try:
        config = load_config()
        tally_url = config["tally_url"]

        if not company_name:
            company_details = get_company_details()
            company_name = company_details.get("company_name", "")

        supabase = create_client(config["supabase_url"], config["supabase_key"])
        now_ts = datetime.now(timezone.utc).isoformat()

        # ── Get last processed alter_id ────────────────────────────────────
        max_alter_id = get_max_alter_id(supabase, company_name)
        logging.info(f"Querying Settled Bills for company: {company_name}...")
        logging.info(f"Settled bills incremental sync from alter_id > {max_alter_id}")

        formatted_records = []

        if max_alter_id == 0:
            # ── FIRST RUN: Filtered load for customers with payment activity ─
            customers = fetch_all_customers(supabase, company_name)
            if not customers:
                logging.info("No customers found. Skipping settled bills.")
                return 0

            # Get current max receipt alter_id from Tally for checkpoint
            affected_for_checkpoint = fetch_affected_ledgers(tally_url, company_name, 0)
            current_alter_id = max(affected_for_checkpoint.values()) if affected_for_checkpoint else 1

            formatted_records = full_load_all_customers(
                tally_url, company_name, customers, current_alter_id, now_ts, affected_for_checkpoint
            )
        else:
            # ── INCREMENTAL: Only process ledgers with new payment activity ─
            affected_ledgers = fetch_affected_ledgers(tally_url, company_name, max_alter_id)

            if not affected_ledgers:
                logging.info("No new payment activity. Settled bills already up to date.")
                return 0

            formatted_records = incremental_load_affected(
                tally_url, company_name, supabase, affected_ledgers, now_ts
            )

        # ── Deduplicate by guid ────────────────────────────────────────────
        unique_records = {r["guid"]: r for r in formatted_records}
        formatted_records = list(unique_records.values())

        logging.info(f"Found {len(formatted_records)} unique valid fully settled bills to insert.")

        # ── 1. Stage into local SQLite table 'ledger_bill_settlements' ─────
        from .local_db import save_to_local_db
        if formatted_records:
            staged = save_to_local_db("ledger_bill_settlements", formatted_records, company_name)
            logging.info(f"[LocalDB] Staged {staged} settled bills locally in sync_cache.db")
            return staged
        return 0

    except Exception as e:
        logging.error(f"Error syncing settled bills: {e}", exc_info=True)
        return 0
