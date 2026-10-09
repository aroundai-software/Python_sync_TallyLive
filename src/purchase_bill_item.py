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
    inventory_tax,
    reconcile_from_id,
    source_number,
)


# =====================================================
# HELPERS
# =====================================================

def clean_text(value):
    if value is None:
        return ""
    value = str(value)
    value = re.sub(r"&#\d+;", "", value)
    return value.strip()


def get_text(value):
    if value is None:
        return ""
    if isinstance(value, list):
        if len(value) > 0:
            return get_text(value[0])
        return ""
    if isinstance(value, dict):
        return clean_text(value.get("#text", ""))
    return clean_text(value)


def parse_float(value, default=0.0):
    return source_number(value, default)


def parse_qty(raw):
    """
    Parse quantity from Tally formats like '5.00 Nos' or '10 Nos = 100.5 kg'.
    Uses source_number so alternate-unit values after '=' are honoured.
    """
    if not raw:
        return 0.0
    text_val = get_text(raw)
    if not text_val:
        return 0.0
    return parse_float(text_val)


def clean_tally_xml(xml_text):
    return clean_source_xml(xml_text)


def fetch_all(supabase, table, columns, company_name=None):
    results = []
    page_size = 1000
    offset = 0

    while True:
        query = supabase.table(table).select(columns)
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


def get_entries(voucher):
    """
    Return all inventory entry dicts from a voucher.
    Tries ALLINVENTORYENTRIES.LIST first, then INVENTORYENTRIES.LIST.
    """
    for key in ["ALLINVENTORYENTRIES.LIST", "INVENTORYENTRIES.LIST"]:
        entries = voucher.get(key)
        if entries:
            if isinstance(entries, dict):
                return [entries]
            if isinstance(entries, list):
                return entries
    return []


def get_batch_entries(entry):
    """Return all BATCHALLOCATIONS.LIST child dicts of an entry."""
    batches = entry.get("BATCHALLOCATIONS.LIST")
    if not batches:
        return []
    if isinstance(batches, dict):
        return [batches]
    return batches


def resolve_quantity(entry):
    qty = abs(
        parse_qty(
            entry.get("ACTUALQTY") or entry.get("BILLEDQTY")
        )
    )
    if qty:
        return qty

    # Fallback: sum batch allocation quantities
    batch_qty = sum(
        abs(parse_qty(b.get("ACTUALQTY") or b.get("BILLEDQTY")))
        for b in get_batch_entries(entry)
    )
    return batch_qty if batch_qty else 0.0


def resolve_rate(entry):
    rate_raw = get_text(entry.get("RATE"))
    if not rate_raw:
        return 0.0
    rate_raw = rate_raw.split("/")[0].strip()
    return abs(parse_float(rate_raw))


def resolve_discount(entry):
    discount_pct = abs(parse_float(entry.get("DISCOUNT")))
    
    discount_amount = sum(
        abs(parse_float(b.get("DISCOUNT") or "0"))
        for b in get_batch_entries(entry)
    )
    
    return round(discount_pct, 2), round(discount_amount, 2)


def resolve_tax(entry, quantity, unit_price):
    return inventory_tax(entry, quantity, unit_price)


# =====================================================
# FETCH TALLY
# =====================================================

def get_purchase_bill_items(company_name: str = None):
    config = load_config()
    tally_url = config["tally_url"]
    supabase = create_client(config["supabase_url"], config["supabase_key"])

    if not company_name:
        company_details = get_company_details()
        company_name = company_details.get("company_name", "")

    from .local_db import get_local_invoice_map, get_max_alter_id_local, save_to_local_db

    logging.info(f"Fetching invoice IDs locally for company: {company_name}...")
    guid_to_invoice_id = get_local_invoice_map("purchase_invoices", company_name)
    if not guid_to_invoice_id:
        try:
            guid_to_invoice_id = {
                row["guid"]: row["id"]
                for row in fetch_all(supabase, "purchase_invoices", "id, guid", company_name=company_name)
            }
        except Exception:
            guid_to_invoice_id = {}
    logging.info(f"Loaded {len(guid_to_invoice_id)} purchase_invoices for {company_name}")

    # One FY window — inventory FETCH makes small date chunks unreliable in Tally.
    from .date_utils import get_current_fy_monthly_date_ranges
    _months = get_current_fy_monthly_date_ranges()
    date_ranges = [(_months[0][0], _months[-1][1])] if _months else []

    # Determine checkpoint locally from SQLite
    max_alter_id = reconcile_from_id(get_max_alter_id_local("invoice_items_purchase", company_name))
    logging.info(f"Purchase items incremental sync from alter_id > {max_alter_id}")
    
    logging.info(
        f"Fetching purchase bill items for FY window "
        f"{date_ranges[0][0]}-{date_ranges[0][1]}..."
        if date_ranges else "Fetching purchase bill items (no FY window)..."
    )
    
    items_list = []
    skipped_no_bill = 0
    skipped_no_items = 0

    total_chunks = len(date_ranges)
    for chunk_idx, (start_date, end_date) in enumerate(date_ranges, 1):
        logging.info(f"[{chunk_idx}/{total_chunks}] Fetching {start_date} to {end_date}...")
        chunk_items = []
        
        alter_filter_tag = "<FILTER>AlterIdFilter</FILTER>" if max_alter_id > 0 else ""
        alter_formula_tag = f'<SYSTEM TYPE="Formulae" NAME="AlterIdFilter">$AlterId &gt; {max_alter_id}</SYSTEM>' if max_alter_id > 0 else ""

        xml_request = f"""
<ENVELOPE>
    <HEADER>
        <VERSION>1</VERSION>
        <TALLYREQUEST>Export</TALLYREQUEST>
        <TYPE>Collection</TYPE>
        <ID>Purchase Bill Items Collection</ID>
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
                    <COLLECTION NAME="Purchase Bill Items Collection">
                        <TYPE>Voucher</TYPE>
                        <FETCH>
                            DATE,
                            GUID,
                            MASTERID,
                            VOUCHERNUMBER,
                            VOUCHERTYPENAME,
                            ISDELETED,
                            ISCANCELLED,
                            ALTERID,
                            ALLINVENTORYENTRIES.LIST,
                            ALLINVENTORYENTRIES.RATEDETAILS.GSTRATE,
                            ALLINVENTORYENTRIES.RATEDETAILS.GSTRATEDUTYHEAD,
                            INVENTORYENTRIES.LIST,
                            INVENTORYENTRIES.RATEDETAILS.GSTRATE,
                            INVENTORYENTRIES.RATEDETAILS.GSTRATEDUTYHEAD
                        </FETCH>
                        <FILTER>PurchaseTypeFilter</FILTER>
                        {alter_filter_tag}
                    </COLLECTION>
                    <SYSTEM TYPE="Formulae" NAME="PurchaseTypeFilter">$$IsPurchase:$VoucherTypeName</SYSTEM>
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
                timeout=300
            )
            response.raise_for_status()

            xml_content = clean_tally_xml(decode_response(response))
            data = xmltodict.parse(xml_content)

            collection = data.get("ENVELOPE", {}).get("BODY", {}).get("DATA", {}).get("COLLECTION", {})
            vouchers = collection.get("VOUCHER", [])
            if isinstance(vouchers, dict):
                vouchers = [vouchers]

            if not vouchers:
                continue

            for voucher in vouchers:
                try:
                    if get_text(voucher.get("ISDELETED")) == "Yes":
                        continue
                    if get_text(voucher.get("ISCANCELLED")) == "Yes":
                        continue

                    guid = get_text(voucher.get("GUID"))
                    if not guid:
                        continue

                    purchase_invoice_id = guid_to_invoice_id.get(guid)
                    if not purchase_invoice_id:
                        skipped_no_bill += 1
                        continue

                    voucher_alter_id = 0
                    try:
                        voucher_alter_id = int(get_text(voucher.get("ALTERID")) or 0)
                    except (ValueError, TypeError):
                        voucher_alter_id = 0

                    entries = get_entries(voucher)
                    if not entries:
                        skipped_no_items += 1
                        continue

                    for idx, entry in enumerate(entries):
                        item_name = get_text(entry.get("STOCKITEMNAME"))
                        if not item_name:
                            continue

                        quantity = int(round(float(resolve_quantity(entry))))
                        unit_price = resolve_rate(entry)
                        discount_pct, discount_amount = resolve_discount(entry)

                        if discount_pct and not discount_amount:
                            discount_amount = round(quantity * unit_price * discount_pct / 100, 2)

                        subtotal = round(
                            abs(source_number(entry.get("AMOUNT")))
                            if get_text(entry.get("AMOUNT"))
                            else (quantity * unit_price) - discount_amount,
                            2,
                        )
                        tax_pct, tax_amount = resolve_tax(entry, quantity, unit_price)
                        line_total = round(subtotal + tax_amount, 2)

                        row = {
                            "purchase_invoice_id": purchase_invoice_id,
                            "product_name": item_name,
                            "product_code": None,
                            "quantity": quantity, # numeric column allows floats
                            "free_quantity": 0,
                            "unit_price": round(unit_price, 4),
                            "gst_rate": tax_pct,
                            "gst_amount": round(tax_amount, 2),
                            "total_amount": line_total,
                            "discount_percentage": discount_pct,
                            "discount_amount": discount_amount,
                            "company_name": company_name,
                            "guid": f"{guid}-{idx}",
                            "alter_id": voucher_alter_id,
                        }
                        items_list.append(row)
                        chunk_items.append(row)
                except Exception as ve:
                    logging.error(
                        f"Voucher Error Purchase Items {get_text(voucher.get('VOUCHERNUMBER')) or '<no number>'} "
                        f"({start_date}-{end_date}): {ve}"
                    )

            # Stage once per date chunk - not after every voucher (that was rewriting
            # the whole week into SQLite hundreds of times and made full reconcile crawl).
            if chunk_items:
                stage_records("purchase_items", chunk_items, company_name)
                logging.info(f"[{chunk_idx}/{total_chunks}] Staged {len(chunk_items)} purchase bill items locally ({start_date}-{end_date})")
            else:
                logging.info(f"[{chunk_idx}/{total_chunks}] 0 new purchase bill items in ({start_date}-{end_date})")

        except Exception as e:
            logging.info(f"Chunk Error ({start_date}-{end_date}): {e}")
        
        # Breathing gap for Tally single-threaded event loop
        time.sleep(0.5)

    logging.info(f"Skipped {skipped_no_bill} vouchers - no matching purchase_invoice in Supabase")
    logging.info(f"Skipped {skipped_no_items} vouchers - no inventory entries found")

    return items_list


# =====================================================
# SUPABASE SAVE
# =====================================================

_has_alter_id_column = None

def check_alter_id_column(supabase):
    global _has_alter_id_column
    if _has_alter_id_column is not None:
        return _has_alter_id_column
    try:
        supabase.table("invoice_items_purchase").select("alter_id").limit(1).execute()
        _has_alter_id_column = True
    except Exception:
        _has_alter_id_column = False
    return _has_alter_id_column


def save_purchase_bill_items_to_supabase(items_list=None, company_name=None):
    if not items_list:
        return 0

    comp_name = company_name or (items_list[0].get("company_name", "") if items_list else "")

    from .local_db import save_to_local_db
    staged = save_to_local_db("invoice_items_purchase", items_list, comp_name)
    logging.info(f"[LocalDB] Staged {staged} purchase bill items locally in sync_cache.db")
    return staged


# =====================================================
# MAIN
# =====================================================

if __name__ == "__main__":
    try:
        logging.info("Fetching purchase bill items from Tally...")
        items = get_purchase_bill_items()
        logging.info(f"Prepared {len(items)} line items.")
        logging.info("Saving to Supabase...")
        save_purchase_bill_items_to_supabase(items)
        logging.info(f"\nPurchase Bill Items synced successfully ({len(items)} records).")
    except Exception as e:
        logging.info(f"Purchase Bill Items sync failed: {e}")
        raise
