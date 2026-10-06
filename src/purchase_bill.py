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

    try:

        value = get_text(value)

        value = value.replace(",", "")

        match = re.search(
            r"-?\d+(\.\d+)?",
            value
        )

        if not match:
            return default

        return float(match.group())

    except Exception:

        return default


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
    """Remove invalid XML character refs and fix stray ampersands."""

    xml_text = re.sub(
        r'&#(?:x[0-9A-Fa-f]+|\d+);',
        '',
        xml_text
    )

    xml_text = re.sub(
        r"&(?!amp;|lt;|gt;|quot;|apos;)",
        "&amp;",
        xml_text
    )

    return xml_text


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
                        <CHILDOF>$$DutiesAndTaxes</CHILDOF>
                        <FETCH>NAME</FETCH>
                    </COLLECTION>
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
            timeout=60
        )
        response.raise_for_status()
        xml_content = clean_tally_xml(response.text)
        data = xmltodict.parse(xml_content)
        
        collection = data.get("ENVELOPE", {}).get("BODY", {}).get("DATA", {}).get("COLLECTION", {})
        ledgers = collection.get("LEDGER", [])
        if isinstance(ledgers, dict):
            ledgers = [ledgers]
            
        tax_names = set()
        for l in ledgers:
            name = get_text(l.get("@NAME"))
            if name:
                tax_names.add(name.strip().lower())
        return tax_names
    except Exception as e:
        logging.error(f"Failed to fetch tax ledgers: {e}")
        return set()


def get_tax_amount(voucher, tax_ledger_names):

    entries = voucher.get("LEDGERENTRIES.LIST") or []

    if isinstance(entries, dict):
        entries = [entries]

    tax = 0.0

    for entry in entries:

        if not isinstance(entry, dict):
            continue

        ledger_name = get_text(
            entry.get("LEDGERNAME")
        ).strip().lower()

        if ledger_name in tax_ledger_names:
            tax += abs(safe_float(entry.get("AMOUNT")))

    return round(tax, 2)


# =====================================================
# SUPPLIER LOOKUP
# =====================================================

def fetch_all_suppliers_cached(company_name: str = None):
    """Page through suppliers from Supabase for target company."""
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

def get_purchase_bills(company_name: str = None):

    config = load_config()
    tally_url = config["tally_url"]
    if not company_name:
        company_details = get_company_details()
        company_name = company_details.get("company_name", "")

    # -------------------------------------------------
    # BUILD SUPPLIER MAPPING (Offline-first from local SQLite)
    # -------------------------------------------------
    from .local_db import get_local_customers_map, get_max_alter_id_local, save_to_local_db
    supplier_map = get_local_customers_map(company_name)
    if not supplier_map:
        try:
            suppliers = fetch_all_suppliers_cached(company_name)
            for supplier in suppliers:
                key = supplier["customer_name"].strip().lower()
                supplier_map[key] = {
                    "id": supplier["id"],
                    "ledger_type": supplier.get("ledger_type")
                }
        except Exception:
            supplier_map = {}
    logging.info(f"Loaded {len(supplier_map)} suppliers for purchase bills")

    # -------------------------------------------------
    # DATE CHUNKING
    # -------------------------------------------------
    
    tax_ledgers = get_tax_ledgers(company_name)
    logging.info(f"Loaded {len(tax_ledgers)} tax ledgers")

    from .date_utils import get_current_fy_monthly_date_ranges
    date_ranges = get_current_fy_monthly_date_ranges()

    # Fetch max alter_id from local SQLite
    max_alter_id = get_max_alter_id_local("purchase_invoices", company_name)
    _last_start_alter_ids[company_name] = max_alter_id
    logging.info(f"Purchase incremental sync from alter_id > {max_alter_id}")
    
    logging.info(f"Fetching purchase bills in {len(date_ranges)} monthly chunks...")
    
    purchase_bills_list = []
    now = datetime.now(timezone.utc).isoformat()
    
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
        <ID>Purchase Bill Collection</ID>
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
                    <COLLECTION NAME="Purchase Bill Collection">
                        <TYPE>Voucher</TYPE>
                        <FETCH>
                            DATE,
                            GUID,
                            MASTERID,
                            VOUCHERNUMBER,
                            VOUCHERTYPENAME,
                            PARTYNAME,
                            AMOUNT,
                            NARRATION,
                            REFERENCE,
                            REFERENCEDATE,
                            ISDELETED,
                            ISCANCELLED,
                            ALTERID
                        </FETCH>
                        <FILTER>DateFilter</FILTER>
                        {alter_filter_tag}
                    </COLLECTION>
                    <SYSTEM TYPE="Formulae" NAME="DateFilter">$Date &gt;= $$Date:"{start_date}" AND $Date &lt;= $$Date:"{end_date}" AND $$IsPurchase:$VoucherTypeName</SYSTEM>
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
                timeout=180
            )
            response.raise_for_status()
            
            xml_content = clean_tally_xml(response.text)
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
            for voucher in vouchers:
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

                supplier_name = get_text(voucher.get("PARTYNAME"))
                supplier_key = supplier_name.strip().lower()
                supplier_info = supplier_map.get(supplier_key, {})
                supplier_id = supplier_info.get("id")
                ledger_type = supplier_info.get("ledger_type")

                grand_total = abs(safe_float(voucher.get("AMOUNT")))
                tax_total = get_tax_amount(voucher, tax_ledgers)
                subtotal = round(grand_total - tax_total, 2)

                row = {
                    "invoice_number": order_number,
                    "invoice_date": date,
                    "supplier_name": supplier_name,
                    "supplier_id": supplier_id,
                    "total_amount": round(grand_total, 2),
                    "gst_amount": round(tax_total, 2),
                    "net_amount": round(subtotal, 2),
                    "discount_amount": 0,
                    "discount_percentage": 0,
                    "subtotal_before_discount": round(subtotal, 2),
                    "status": "Pending",
                    "type": get_text(voucher.get("VOUCHERTYPENAME")),
                    "notes": get_text_or_none(voucher.get("NARRATION")),
                    "remarks": get_text_or_none(voucher.get("REFERENCE")),
                    "company_name": company_name,
                    "ledger_type": ledger_type,
                    "round_off": 0,
                    "synced_to_tally": True,
                    "guid": guid,
                    "master_id": str(master_id) if master_id else None,
                    "alter_id": int(safe_float(voucher.get("ALTERID"), 0)),
                    "updated_at": now,
                }

                purchase_bills_list.append(row)
                chunk_bills.append(row)
                
            if cancelled_guids:
                from .local_db import stage_deletions
                stage_deletions("purchase_invoices", cancelled_guids, company_name)
                logging.info(f"Staged {len(cancelled_guids)} cancelled purchase vouchers for deletion")

            if chunk_bills:
                stage_records("purchases", chunk_bills, company_name)
                logging.info(f"[{chunk_idx}/{total_chunks}] Staged {len(chunk_bills)} purchase bills locally ({start_date}-{end_date})")
            else:
                logging.info(f"[{chunk_idx}/{total_chunks}] 0 new purchase bills in ({start_date}-{end_date})")

        except Exception as e:
            logging.info(f"Chunk Error ({start_date}-{end_date}): {e}")
            
        # Breathing gap — gives Tally's event loop time to flush memory between chunks
        time.sleep(0.5)

    # Deduplicate by GUID to prevent batch upsert conflicts
    unique_bills = {}
    for bill in purchase_bills_list:
        guid = bill.get("guid")
        if guid:
            unique_bills[guid] = bill

    return list(unique_bills.values())


# =====================================================
# SUPABASE SAVE
# =====================================================

def save_purchase_bills_to_supabase(purchase_bills=None, company_name=None):
    if not purchase_bills:
        return 0

    comp_name = company_name or (purchase_bills[0].get("company_name", "") if purchase_bills else "")

    from .local_db import save_to_local_db
    staged = save_to_local_db("purchase_invoices", purchase_bills, comp_name)
    logging.info(f"[LocalDB] Staged {staged} purchase bills locally in sync_cache.db")
    return staged


def reconcile_deleted_purchase_bills(company_name: str) -> int:
    """
    Fetches all active Purchase Voucher GUIDs from Tally for the current FY,
    compares them against Supabase 'purchase_invoices', and deletes any records
    (and their corresponding 'invoice_items_purchase') that were deleted in Tally.
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
        <ID>AllPurchaseGuids</ID>
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
                    <COLLECTION NAME="AllPurchaseGuids">
                        <TYPE>Voucher</TYPE>
                        <FETCH>GUID,ISDELETED,ISCANCELLED</FETCH>
                        <FILTER>PurchaseTypeFilter</FILTER>
                    </COLLECTION>
                    <SYSTEM TYPE="Formulae" NAME="PurchaseTypeFilter">$$IsPurchase:$VoucherTypeName</SYSTEM>
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
            timeout=120
        )
        response.raise_for_status()
        xml_content = clean_tally_xml(response.text)
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
        logging.warning(f"Could not fetch active Purchase GUIDs from Tally for reconciliation: {e}")
        return 0

    if not tally_active_guids:
        logging.warning("Reconcile: Tally returned 0 active Purchase GUIDs — skipping deletion to prevent data wipe.")
        return 0

    from .local_db import reconcile_local_deletions
    return reconcile_local_deletions("purchase_invoices", tally_active_guids, company_name)



# =====================================================
# MAIN
# =====================================================

if __name__ == "__main__":

    try:

        logging.info("Fetching purchase bills from Tally...")

        purchase_bills = get_purchase_bills()

        logging.info(f"Found {len(purchase_bills)} purchase bills.")

        logging.info("Saving to Supabase...")

        save_purchase_bills_to_supabase(purchase_bills)

        logging.info(
            f"\nPurchase Bills synced successfully ({len(purchase_bills)} records)."
        )

    except Exception as e:

        logging.info(f"Purchase Bill sync failed: {e}")

        raise
