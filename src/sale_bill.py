import logging
import re
import requests
import time
import xmltodict
from datetime import datetime, timezone
from xml.sax.saxutils import escape

from supabase import create_client

from .config import load_config
from .tally_companies import get_company_details
from .local_db import stage_records, get_pending_records, mark_as_synced, mark_as_failed
from .source_values import (
    clean_source_xml,
    decode_response,
    gst_names_xml,
    invoice_tax,
    reconcile_from_id,
    source_number,
)
from .tally_timeouts import tally_http_timeout


# =====================================================
# HELPERS
# =====================================================

def clean_text(value):

    if value is None:
        return ""

    value = str(value)

    value = re.sub(
        r"&#\d+;",
        "",
        value
    )

    return value.strip()


def get_text(value):

    if value is None:
        return ""

    if isinstance(value, list):

        if len(value) > 0:
            return get_text(value[0])

        return ""

    if isinstance(value, dict):

        return clean_text(
            value.get("#text", "")
        )

    return clean_text(value)


def get_text_or_none(value):
    """Return text or None if empty."""
    text = get_text(value)
    return text if text else None


def safe_float(value, default=0):
    return source_number(value, default)


def format_tally_date(value):

    value = get_text(value)

    if not value or len(value) != 8:
        return None

    try:

        return datetime.strptime(
            value,
            "%Y%m%d"
        ).date().isoformat()

    except Exception:

        return None


def clean_tally_xml(xml_text):
    return clean_source_xml(xml_text)


def get_tax_ledgers(company_name: str):
    xml_request = f"""
<ENVELOPE>
    <HEADER>
        <VERSION>1</VERSION>
        <TALLYREQUEST>Export</TALLYREQUEST>
        <TYPE>Collection</TYPE>
        <ID>TaxLedgers</ID>
    </HEADER>
    <BODY>
        <DESC>
            <STATICVARIABLES>
                <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
                <SVCURRENTCOMPANY>{escape(company_name)}</SVCURRENTCOMPANY>
            </STATICVARIABLES>
            <TDL>
                <TDLMESSAGE>
                    <COLLECTION NAME="TaxLedgers">
                        <TYPE>Ledger</TYPE>
                        <FETCH>NAME,TAXTYPE</FETCH>
                        <FILTER>GSTTaxLedgers</FILTER>
                        <BELONGSTO>Yes</BELONGSTO>
                    </COLLECTION>
                    <SYSTEM TYPE="Formulae" NAME="GSTTaxLedgers">$TaxType = "GST"</SYSTEM>
                </TDLMESSAGE>
            </TDL>
        </DESC>
    </BODY>
</ENVELOPE>
"""
    config = load_config()
    try:
        response = requests.post(
            config["tally_url"],
            data=xml_request.encode("utf-8"),
            headers={"Content-Type": "application/xml"},
            timeout=tally_http_timeout(local_read=60, remote_read=180),
        )
        response.raise_for_status()
        return gst_names_xml(decode_response(response))
    except Exception as e:
        # Returning an empty set here would silently zero the GST on every
        # invoice in this cycle, so the failure has to stop the module.
        logging.error(f"Failed to fetch tax ledgers: {e}")
        raise


def get_tax_amount(voucher, tax_ledger_names):
    return invoice_tax(voucher, tax_ledger_names, purchase=False)


# =====================================================
# CUSTOMER LOOKUP
# =====================================================

def fetch_all_customers_cached(company_name: str = None):
    """Page through customers from Supabase for target company."""
    config = load_config()
    supabase = create_client(config["supabase_url"], config["supabase_key"])
    
    results = []
    page_size = 1000
    offset = 0

    while True:
        query = supabase.table("customers").select("id, customer_name, ledger_type")
        if company_name:
            query = query.eq("company_name", company_name)
        rows = (
            query.range(offset, offset + page_size - 1)
            .execute()
            .data or []
        )
        results.extend(rows)

        if len(rows) < page_size:
            break

        offset += page_size

    return results


_last_start_alter_ids = {}

def get_last_start_alter_id(company_name: str) -> int:
    return _last_start_alter_ids.get(company_name, 0)


# =====================================================
# TALLY FETCH
# =====================================================

def get_sale_bills(company_name: str = None):

    config = load_config()
    tally_url = config["tally_url"]
    supabase = create_client(config["supabase_url"], config["supabase_key"])
    if not company_name:
        company_details = get_company_details()
        company_name = company_details.get("company_name", "")
    now = datetime.now(timezone.utc).isoformat()

    # -------------------------------------------------
    # BUILD CUSTOMER MAPPING (Offline-first from local SQLite)
    # -------------------------------------------------
    from .local_db import get_local_customers_map, get_max_alter_id_local, save_to_local_db
    customer_map = get_local_customers_map(company_name)
    if not customer_map:
        try:
            customers = fetch_all_customers_cached(company_name)
            for customer in customers:
                key = customer["customer_name"].strip().lower()
                customer_map[key] = {
                    "id": customer["id"],
                    "ledger_type": customer.get("ledger_type")
                }
        except Exception:
            customer_map = {}
    logging.info(f"Loaded {len(customer_map)} customers for sale bills")

    # -------------------------------------------------
    # DATE CHUNKING
    # -------------------------------------------------
    
    tax_ledgers = get_tax_ledgers(company_name)
    logging.info(f"Loaded {len(tax_ledgers)} tax ledgers")

    from .date_utils import get_current_fy_monthly_date_ranges
    date_ranges = get_current_fy_monthly_date_ranges()

    # Fetch max alter_id from local SQLite to enable incremental sync
    max_alter_id = reconcile_from_id(get_max_alter_id_local("sales_invoices", company_name))
    _last_start_alter_ids[company_name] = max_alter_id
    logging.info(f"Sales incremental sync from alter_id > {max_alter_id}")
    
    logging.info(f"Fetching sale bills in {len(date_ranges)} monthly chunks...")
    
    sale_bills_list = []
    
    total_chunks = len(date_ranges)
    for chunk_idx, (start_date, end_date) in enumerate(date_ranges, 1):
        logging.info(f"[{chunk_idx}/{total_chunks}] Fetching {start_date} to {end_date}...")
        chunk_bills = []
        
        alter_filter_tag = "<FILTER>AlterIdFilter</FILTER>" if max_alter_id > 0 else ""
        alter_formula_tag = f'<SYSTEM TYPE="Formulae" NAME="AlterIdFilter">$AlterId &gt; {max_alter_id}</SYSTEM>' if max_alter_id > 0 else ""

        xml_request = f"""
<ENVELOPE>
    <HEADER>
        <VERSION>1</VERSION>
        <TALLYREQUEST>Export</TALLYREQUEST>
        <TYPE>Collection</TYPE>
        <ID>Sale Bill Collection</ID>
    </HEADER>
    <BODY>
        <DESC>
            <STATICVARIABLES>
                <SVFROMDATE TYPE="Date">{start_date}</SVFROMDATE>
                <SVTODATE TYPE="Date">{end_date}</SVTODATE>
                <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
                <SVCURRENTCOMPANY>{escape(company_name)}</SVCURRENTCOMPANY>
            </STATICVARIABLES>
            <TDL>
                <TDLMESSAGE>
                    <COLLECTION NAME="Sale Bill Collection">
                        <TYPE>Voucher</TYPE>
                        <FETCH>
                            DATE,
                            GUID,
                            MASTERID,
                            VOUCHERNUMBER,
                            VOUCHERTYPENAME,
                            PARTYNAME,
                            PARTYLEDGERNAME,
                            AMOUNT,
                            NARRATION,
                            REFERENCE,
                            REFERENCEDATE,
                            BASICDUEDATEOFPYMT,
                            BASICBUYERNAME,
                            BASICBUYERADDRESS.LIST,
                            LEDGERENTRIES.LIST,
                            ALLLEDGERENTRIES.LIST,
                            ISDELETED,
                            ISCANCELLED,
                            ALTERID
                        </FETCH>
                        <FILTER>SalesTypeFilter</FILTER>
                        {alter_filter_tag}
                    </COLLECTION>
                    <SYSTEM TYPE="Formulae" NAME="SalesTypeFilter">$$IsSales:$VoucherTypeName</SYSTEM>
                    {alter_formula_tag}
                </TDLMESSAGE>
            </TDL>
        </DESC>
    </BODY>
</ENVELOPE>
"""

        try:
            response = requests.post(
                tally_url,
                data=xml_request.encode("utf-8"),
                headers={"Content-Type": "application/xml", "Connection": "close"},
                timeout=tally_http_timeout(local_read=180, remote_read=300),
            )
            response.raise_for_status()
            
            xml_content = clean_tally_xml(decode_response(response))
            data = xmltodict.parse(xml_content)
            
            collection = (
                data.get("ENVELOPE", {})
                .get("BODY", {})
                .get("DATA", {})
                .get("COLLECTION", {})
            )

            vouchers = collection.get("VOUCHER", [])
            if isinstance(vouchers, dict):
                vouchers = [vouchers]
                
            if not vouchers:
                continue

            cancelled_guids = []
            skipped_vouchers = 0
            for voucher in vouchers:
                try:
                    if get_text(voucher.get("ISDELETED")) == "Yes" or get_text(voucher.get("ISCANCELLED")) == "Yes":
                        v_guid = get_text(voucher.get("GUID"))
                        if v_guid:
                            cancelled_guids.append(v_guid)
                        continue

                    order_number = get_text(voucher.get("VOUCHERNUMBER"))
                    if not order_number:
                        continue

                    guid = get_text(voucher.get("GUID"))
                    master_id = int(safe_float(voucher.get("MASTERID")))
                    date = format_tally_date(voucher.get("DATE"))

                    if not date:
                        continue

                    customer_name = get_text(voucher.get("PARTYLEDGERNAME")) or get_text(voucher.get("PARTYNAME"))
                    customer_key = customer_name.strip().lower()
                    customer_info = customer_map.get(customer_key, {})
                    customer_id = customer_info.get("id")
                    ledger_type = customer_info.get("ledger_type")

                    address = get_address(voucher) if 'get_address' in globals() else None
                    grand_total = abs(safe_float(voucher.get("AMOUNT")))
                    tax_total = get_tax_amount(voucher, tax_ledgers)
                    subtotal = round(grand_total - tax_total, 2)

                    row = {
                        "invoice_number": order_number,
                        "invoice_date": date,
                        "customer_name": customer_name,
                        "customer_id": customer_id,
                        "total_amount": round(grand_total, 2),
                        "gst_amount": round(tax_total, 2),
                        "net_amount": round(subtotal, 2),
                        "discount_amount": 0,
                        "discount_percentage": 0,
                        "subtotal_before_discount": round(subtotal, 2),
                        # Provisional; corrected from outstanding by invoice_status before cloud push
                        "status": "Pending",
                        "Type": get_text(voucher.get("VOUCHERTYPENAME")),
                        "notes": get_text_or_none(voucher.get("NARRATION")),
                        "remarks": get_text_or_none(voucher.get("REFERENCE")),
                        "shipping_address": address or None,
                        "company_name": company_name,
                        "ledger_type": ledger_type,
                        "round_off": None,
                        "synced_to_tally": True,
                        "guid": guid,
                        "master_id": str(master_id) if master_id else None,
                        "alter_id": int(safe_float(voucher.get("ALTERID"), 0)),
                        "updated_at": now,
                    }

                    sale_bills_list.append(row)
                    chunk_bills.append(row)
                except Exception as ve:
                    skipped_vouchers += 1
                    logging.error(
                        f"Voucher Error Sale {get_text(voucher.get('VOUCHERNUMBER')) or '<no number>'} "
                        f"({start_date}-{end_date}): {ve}"
                    )

            if skipped_vouchers:
                logging.error(
                    f"[{chunk_idx}/{total_chunks}] Skipped {skipped_vouchers} unparseable sale "
                    f"vouchers in ({start_date}-{end_date})"
                )

            if cancelled_guids:
                from .local_db import stage_deletions
                stage_deletions("sales_invoices", cancelled_guids, company_name)
                logging.info(f"Staged {len(cancelled_guids)} cancelled sale vouchers for deletion")

            if chunk_bills:
                stage_records("sales", chunk_bills, company_name)
                logging.info(f"[{chunk_idx}/{total_chunks}] Staged {len(chunk_bills)} sale bills locally ({start_date}-{end_date})")
            else:
                logging.info(f"[{chunk_idx}/{total_chunks}] 0 new sale bills in ({start_date}-{end_date})")

        except Exception as e:
            logging.info(f"Chunk Error ({start_date}-{end_date}): {e}")
            
        # Breathing gap - gives Tally's event loop time to flush memory between chunks
        time.sleep(0.5)

    # Deduplicate by GUID to prevent batch upsert conflicts
    unique_bills = {}
    for bill in sale_bills_list:
        guid = bill.get("guid")
        if guid:
            unique_bills[guid] = bill

    return list(unique_bills.values())


# =====================================================
# SUPABASE SAVE
# =====================================================

def save_sale_bills_to_supabase(sale_bills=None, company_name=None):
    if not sale_bills:
        return 0

    comp_name = company_name or (sale_bills[0].get("company_name", "") if sale_bills else "")

    from .local_db import save_to_local_db
    staged = save_to_local_db("sales_invoices", sale_bills, comp_name)
    logging.info(f"[LocalDB] Staged {staged} sales bills locally in sync_cache.db")
    return staged


def reconcile_deleted_sale_bills(company_name: str) -> int:
    """
    Fetches all active Sales Voucher GUIDs from Tally for the current FY,
    compares them against Supabase 'sales_invoices', and deletes any records
    (and their corresponding 'invoice_items') that were deleted in Tally.
    """
    config = load_config()
    tally_url = config.get("tally_url", "http://localhost:9000")
    from .date_utils import get_current_fy_monthly_date_ranges
    date_ranges = get_current_fy_monthly_date_ranges()
    if not date_ranges:
        return 0

    fy_start = date_ranges[0][0]
    fy_end = date_ranges[-1][1]
    fy_start_iso = f"{fy_start[:4]}-{fy_start[4:6]}-{fy_start[6:8]}"

    xml_request = f"""<ENVELOPE>
    <HEADER>
        <VERSION>1</VERSION>
        <TALLYREQUEST>Export</TALLYREQUEST>
        <TYPE>Collection</TYPE>
        <ID>AllSaleGuids</ID>
    </HEADER>
    <BODY>
        <DESC>
            <STATICVARIABLES>
                <SVFROMDATE TYPE="Date">{fy_start}</SVFROMDATE>
                <SVTODATE TYPE="Date">{fy_end}</SVTODATE>
                <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
                <SVCURRENTCOMPANY>{escape(company_name)}</SVCURRENTCOMPANY>
            </STATICVARIABLES>
            <TDL>
                <TDLMESSAGE>
                    <COLLECTION NAME="AllSaleGuids">
                        <TYPE>Voucher</TYPE>
                        <FETCH>GUID,ISDELETED,ISCANCELLED</FETCH>
                        <FILTER>SalesTypeFilter</FILTER>
                    </COLLECTION>
                    <SYSTEM TYPE="Formulae" NAME="SalesTypeFilter">$$IsSales:$VoucherTypeName</SYSTEM>
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
            timeout=tally_http_timeout(local_read=120, remote_read=240),
        )
        response.raise_for_status()
        xml_content = clean_tally_xml(decode_response(response))
        data = xmltodict.parse(xml_content)
        collection = (
            data.get("ENVELOPE", {})
            .get("BODY", {})
            .get("DATA", {})
            .get("COLLECTION", {})
        )
        vouchers = collection.get("VOUCHER", [])
        if isinstance(vouchers, dict):
            vouchers = [vouchers]
        
        tally_active_guids = set()
        for v in vouchers:
            if not isinstance(v, dict):
                continue
            if get_text(v.get("ISDELETED")) == "Yes" or get_text(v.get("ISCANCELLED")) == "Yes":
                continue
            g = get_text(v.get("GUID"))
            if g:
                tally_active_guids.add(g)

    except Exception as e:
        logging.warning(f"Could not fetch active Sales GUIDs from Tally for reconciliation: {e}")
        return 0

    if not tally_active_guids:
        logging.warning("Reconcile: Tally returned 0 active Sales GUIDs - skipping deletion to prevent data wipe.")
        return 0

    from .local_db import reconcile_local_deletions
    return reconcile_local_deletions("sales_invoices", tally_active_guids, company_name)



# =====================================================
# MAIN
# =====================================================

if __name__ == "__main__":

    try:

        logging.info("Fetching sale bills from Tally...")

        sale_bills = get_sale_bills()

        logging.info(f"Found {len(sale_bills)} sale bills.")

        logging.info("Saving to Supabase...")

        save_sale_bills_to_supabase(sale_bills)

        logging.info(
            f"\nSale Bills synced successfully ({len(sale_bills)} records)."
        )

    except Exception as e:

        logging.info(f"Sale Bill sync failed: {e}")

        raise
