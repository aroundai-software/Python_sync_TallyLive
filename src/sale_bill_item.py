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
    Parse quantity from Tally format e.g. '5.00 Nos' â†’ 5.0.
    Tally appends the unit name after a space â€” take only the numeric part.
    """
    if not raw:
        return 0.0
    text_val = get_text(raw)
    if not text_val:
        return 0.0
    return parse_float(text_val.strip().split()[0])


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

def get_sale_bill_items(company_name: str = None):
    config = load_config()
    tally_url = config["tally_url"]
    supabase = create_client(config["supabase_url"], config["supabase_key"])

    if not company_name:
        company_details = get_company_details()
        company_name = company_details.get("company_name", "")

    from .local_db import get_local_invoice_map, get_local_stock_item_master_map, get_max_alter_id_local, save_to_local_db

    logging.info(f"Fetching invoice IDs locally for company: {company_name}...")
    guid_to_invoice_id = get_local_invoice_map("sales_invoices", company_name)
    if not guid_to_invoice_id:
        try:
            guid_to_invoice_id = {
                row["guid"]: row["id"]
                for row in fetch_all(supabase, "sales_invoices", "id, guid", company_name=company_name)
            }
        except Exception:
            guid_to_invoice_id = {}
    logging.info(f"Loaded {len(guid_to_invoice_id)} sales_invoices for {company_name}")

    logging.info(f"Fetching stock_items locally for company: {company_name}...")
    item_name_to_master = get_local_stock_item_master_map(company_name)
    if not item_name_to_master:
        try:
            for row in fetch_all(supabase, "stock_items", "master_id, ItemName", company_name=company_name):
                key = (row.get("ItemName") or "").strip().lower()
                item_name_to_master[key] = str(row["master_id"]) if row.get("master_id") else None
        except Exception:
            item_name_to_master = {}
    logging.info(f"Loaded {len(item_name_to_master)} stock items for {company_name}")

    from .date_utils import get_current_fy_item_date_ranges
    date_ranges = get_current_fy_item_date_ranges(days_per_chunk=7)

    # Determine checkpoint locally from SQLite
    max_alter_id = reconcile_from_id(get_max_alter_id_local("invoice_items", company_name))
    logging.info(f"Sale items incremental sync from alter_id > {max_alter_id}")
    
    logging.info(f"Fetching sale bill items in {len(date_ranges)} chunks (7-day intervals)...")
    
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
        <ID>Sale Bill Items Collection</ID>
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
                    <COLLECTION NAME="Sale Bill Items Collection">
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
                        <FILTER>DateFilter</FILTER>
                        {alter_filter_tag}
                    </COLLECTION>
                    <SYSTEM TYPE="Formulae" NAME="DateFilter">$Date &gt;= $$Date:"{start_date}" AND $Date &lt;= $$Date:"{end_date}" AND $$IsSales:$VoucherTypeName</SYSTEM>
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
                if get_text(voucher.get("ISDELETED")) == "Yes":
                    continue
                if get_text(voucher.get("ISCANCELLED")) == "Yes":
                    continue

                guid = get_text(voucher.get("GUID"))
                if not guid:
                    continue

                invoice_id = guid_to_invoice_id.get(guid)
                if not invoice_id:
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

                    item_master_id = item_name_to_master.get(item_name.strip().lower())
                    quantity = resolve_quantity(entry)
                    if not float(quantity).is_integer():
                        raise ValueError("Fractional sales quantity requires a numeric quantity column")
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
                        "invoice_id": invoice_id,
                        "product_name": item_name,
                        "product_code": item_master_id,
                        "quantity": int(quantity),
                        "free_quantity": 0,
                        "unit_price": round(unit_price, 4),
                        "gst_rate": tax_pct,
                        "gst_amount": round(tax_amount, 2),
                        "total_amount": line_total,
                        "discount_percentage": discount_pct,
                        "discount_amount": discount_amount,
                        "category_discount_percentage": 0,
                        "company_name": company_name,
                        "guid": f"{guid}-{idx}",
                        "master_id": item_master_id,
                        "alter_id": voucher_alter_id,
                    }
                    items_list.append(row)
                    chunk_items.append(row)

                if chunk_items:
                    stage_records("sale_items", chunk_items, company_name)
                    logging.info(f"[{chunk_idx}/{total_chunks}] Staged {len(chunk_items)} sale bill items locally ({start_date}-{end_date})")
                else:
                    logging.info(f"[{chunk_idx}/{total_chunks}] 0 new sale bill items in ({start_date}-{end_date})")

        except Exception as e:
            logging.info(f"Chunk Error ({start_date}-{end_date}): {e}")
        
        # Breathing gap for Tally single-threaded event loop
        time.sleep(0.5)

    logging.info(f"Skipped {skipped_no_bill} vouchers â€” no matching sales_invoice in Supabase")
    logging.info(f"Skipped {skipped_no_items} vouchers â€” no inventory entries found")

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
        supabase.table("invoice_items").select("alter_id").limit(1).execute()
        _has_alter_id_column = True
    except Exception:
        _has_alter_id_column = False
    return _has_alter_id_column


def save_sale_bill_items_to_supabase(items_list=None, company_name=None):
    if not items_list:
        return 0

    comp_name = company_name or (items_list[0].get("company_name", "") if items_list else "")

    from .local_db import save_to_local_db
    staged = save_to_local_db("invoice_items", items_list, comp_name)
    logging.info(f"[LocalDB] Staged {staged} sale bill items locally in sync_cache.db")
    return staged


# =====================================================
# MAIN
# =====================================================

if __name__ == "__main__":
    try:
        logging.info("Fetching sale bill items from Tally...")
        items = get_sale_bill_items()
        logging.info(f"Prepared {len(items)} line items.")
        logging.info("Saving to Supabase...")
        save_sale_bill_items_to_supabase(items)
        logging.info(f"\nSale Bill Items synced successfully ({len(items)} records).")
    except Exception as e:
        logging.info(f"Sale Bill Items sync failed: {e}")
        raise
