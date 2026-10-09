import logging
import re
import requests
import time
import xmltodict
from datetime import datetime, timezone
from xml.sax.saxutils import escape

from supabase import create_client

from .config import load_config
from .source_values import (
    clean_source_xml,
    decode_response,
    effective_dict,
    reconcile_from_id,
    source_number,
)
from .tally_companies import get_company_details


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


def safe_float(value, default=0):
    """Native Tally numeric read: honours Dr/Cr suffixes and converted INR values."""
    return source_number(value, default)


def clean_tally_xml(xml_text):
    """Strip only illegal control characters, keeping legal character references."""
    return clean_source_xml(xml_text)


def extract_gst_rate(item):
    """Helper to extract GST Rate from modern Tally Prime tags."""
    rate = safe_float(item.get("IGSTRATE"), 0)
    if rate > 0:
        return rate
        
    gst_details = item.get("GSTDETAILS.LIST")
    if not gst_details:
        return 0.0
        
    if isinstance(gst_details, dict):
        gst_details = [gst_details]
        
    for detail in gst_details:
        state_wise = detail.get("STATEWISEDETAILS.LIST")
        if not state_wise:
            continue
            
        if isinstance(state_wise, dict):
            state_wise = [state_wise]
            
        for state in state_wise:
            rate_details = state.get("RATEDETAILS.LIST")
            if not rate_details:
                continue
                
            if isinstance(rate_details, dict):
                rate_details = [rate_details]
                
            for rate_detail in rate_details:
                duty_head = get_text(rate_detail.get("GSTRATEDUTYHEAD"))
                if duty_head in ["IGST", "Integrated Tax"]:
                    r = safe_float(rate_detail.get("GSTRATE"), 0)
                    if r > 0:
                        return r
                        
    return 0.0


# =====================================================
# TALLY FETCH
# =====================================================

def get_stock_group_gst_rates(company_name: str = "Unknown"):
    xml_request = f"""
<ENVELOPE>
    <HEADER>
        <VERSION>1</VERSION>
        <TALLYREQUEST>Export</TALLYREQUEST>
        <TYPE>Collection</TYPE>
        <ID>Stock Group Collection</ID>
    </HEADER>
    <BODY>
        <DESC>
            <STATICVARIABLES>
                <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
                <SVCURRENTCOMPANY>{escape(company_name)}</SVCURRENTCOMPANY>
            </STATICVARIABLES>
            <TDL>
                <TDLMESSAGE>
                    <COLLECTION NAME="Stock Group Collection">
                        <TYPE>StockGroup</TYPE>
                        <FETCH>NAME, PARENT, IGSTRATE, GSTDETAILS.LIST</FETCH>
                    </COLLECTION>
                </TDLMESSAGE>
            </TDL>
        </DESC>
    </BODY>
</ENVELOPE>
"""
    config = load_config()
    tally_url = config["tally_url"]
    
    try:
        response = requests.post(
            tally_url,
            data=xml_request.encode("utf-8"),
            headers={"Content-Type": "application/xml", "Connection": "close"},
            timeout=120
        )
        response.raise_for_status()
        response_text = clean_tally_xml(decode_response(response))
        data = xmltodict.parse(response_text)
    except Exception as e:
        logging.error(
            f"Error fetching Stock Group GST rates for {company_name}; "
            f"group-inherited GST rates will be missing: {e}"
        )
        return {}
        
    collection = data.get("ENVELOPE", {}).get("BODY", {}).get("DATA", {}).get("COLLECTION", {})
    stock_groups = collection.get("STOCKGROUP", [])
    
    if isinstance(stock_groups, dict):
        stock_groups = [stock_groups]
        
    group_rates = {}
    for group in stock_groups:
        name = get_text(group.get("@NAME"))
        if not name:
            continue
        try:
            rate = extract_gst_rate(group)
        except Exception as e:
            logging.error(f"Error reading GST rate for stock group '{name}': {e}")
            continue
        if rate > 0:
            group_rates[name] = rate


    # Deep parent inheritance resolution (recursive)
    changed = True
    while changed:
        changed = False
        for group in stock_groups:
            name = get_text(group.get("@NAME"))
            parent = get_text(group.get("PARENT"))
            if name and not group_rates.get(name) and parent:
                if group_rates.get(parent):
                    group_rates[name] = group_rates[parent]
                    changed = True
                
    return group_rates

def get_max_alter_id(table_name: str, company_name: str) -> int:
    try:
        config = load_config()
        supabase = create_client(config["supabase_url"], config["supabase_key"])
        response = supabase.table(table_name).select("alter_id").eq("company_name", company_name).order("alter_id", desc=True).limit(1).execute()
        if response.data and len(response.data) > 0:
            val = response.data[0].get("alter_id")
            return int(val) if val else 0
    except Exception as e:
        logging.error(f"Failed to fetch max alter_id for {table_name}: {e}")
    return 0

def _integer_quantity(qty) -> int:
    """ItemQuantity is an integer column; round weight-based fractional qty."""
    return int(round(float(qty)))


def _qty_rate_from_closing(item) -> tuple:
    """
    Build signed qty + positive rate so (qty * rate) equals Tally CLOSINGVALUE.
    Stock Summary Value is the source of truth; rate is derived from it.
    """
    qty = safe_float(item.get("CLOSINGBALANCE"), 0)
    val = safe_float(item.get("CLOSINGVALUE"), 0)
    rate = abs(safe_float(item.get("CLOSINGRATE"), 0))
    if qty != 0 and val != 0:
        rate = abs(val / qty)
    elif qty == 0:
        rate = abs(rate)
    qty_out = _integer_quantity(qty)
    # Re-derive rate after rounding so ClosingValue stays the source of truth.
    if qty_out != 0 and val != 0:
        rate = abs(val / qty_out)
    # Keep enough decimals so sum(qty*rate) matches Tally to the paisa
    rate_out = round(rate, 6)
    return qty_out, rate_out


def sync_stock_item_balances(company_name: str):
    """
    Full stock balance refresh as of today.
    Stock qty often changes via vouchers without bumping stock-item ALTERID,
    so this must run every sync cycle (not only on incremental alter_id).
    Always writes and marks pending so Phase 3 pushes live qty/rate to Supabase.
    """
    try:
        from .local_db import get_connection, _require_company_name
        company_name = _require_company_name(company_name, "stock_balances")
        config = load_config()
        tally_url = config["tally_url"]
        today_str = datetime.now().strftime("%Y%m%d")

        xml_request = f"""
<ENVELOPE>
    <HEADER>
        <VERSION>1</VERSION>
        <TALLYREQUEST>Export</TALLYREQUEST>
        <TYPE>Collection</TYPE>
        <ID>Stock Item Balances</ID>
    </HEADER>
    <BODY>
        <DESC>
            <STATICVARIABLES>
                <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
                <SVCURRENTCOMPANY>{escape(company_name)}</SVCURRENTCOMPANY>
                <SVFROMDATE TYPE="Date">20000101</SVFROMDATE>
                <SVTODATE TYPE="Date">{today_str}</SVTODATE>
            </STATICVARIABLES>
            <TDL>
                <TDLMESSAGE>
                    <COLLECTION NAME="Stock Item Balances">
                        <TYPE>Stock Item</TYPE>
                        <FETCH>NAME, GUID, CLOSINGBALANCE, CLOSINGRATE, CLOSINGVALUE</FETCH>
                    </COLLECTION>
                </TDLMESSAGE>
            </TDL>
        </DESC>
    </BODY>
</ENVELOPE>
"""
        response = requests.post(
            tally_url,
            data=xml_request.encode("utf-8"),
            headers={"Content-Type": "application/xml", "Connection": "close"},
            timeout=120
        )
        response.raise_for_status()
        data = xmltodict.parse(clean_tally_xml(decode_response(response)))
        collection = data.get("ENVELOPE", {}).get("BODY", {}).get("DATA", {}).get("COLLECTION", {})
        items = collection.get("STOCKITEM", [])
        if isinstance(items, dict):
            items = [items]

        if not items:
            logging.warning(f"[LocalDB] Stock balance feed returned 0 items for {company_name}.")
            return 0

        conn = get_connection()
        updated_count = 0
        unmatched = 0
        unreadable = 0
        try:
            # Ensure ItemQuantity / ItemRate columns exist even if masters were never staged
            cols = {r["name"].lower() for r in conn.execute('PRAGMA table_info("stock_items")').fetchall()} if conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='stock_items'"
            ).fetchone() else set()
            if not cols:
                logging.warning(f"[LocalDB] No local stock_items table yet for {company_name}; skip balance write.")
                return 0
            with conn:
                if "itemquantity" not in cols:
                    conn.execute('ALTER TABLE "stock_items" ADD COLUMN "ItemQuantity" REAL DEFAULT 0')
                if "itemrate" not in cols:
                    conn.execute('ALTER TABLE "stock_items" ADD COLUMN "ItemRate" REAL DEFAULT 0')
                if "itemname" not in cols:
                    conn.execute('ALTER TABLE "stock_items" ADD COLUMN "ItemName" TEXT')

                for it in items:
                    name = get_text(it.get("@NAME") or it.get("NAME"))
                    try:
                        qty_int, rate = _qty_rate_from_closing(it)
                    except Exception as e:
                        # One unreadable item must not abandon every other balance.
                        unreadable += 1
                        logging.error(f"Error reading stock balance for '{name}': {e}")
                        continue
                    guid = get_text(it.get("GUID"))
                    cur = None
                    if guid:
                        cur = conn.execute(
                            'UPDATE "stock_items" SET "ItemQuantity" = ?, "ItemRate" = ?, '
                            '_sync_status = "pending", _synced_at = NULL '
                            'WHERE company_name = ? AND guid = ?',
                            (qty_int, rate, company_name, guid),
                        )
                    if (cur is None or cur.rowcount == 0) and name:
                        cur = conn.execute(
                            'UPDATE "stock_items" SET "ItemQuantity" = ?, "ItemRate" = ?, '
                            '_sync_status = "pending", _synced_at = NULL '
                            'WHERE company_name = ? AND "ItemName" = ?',
                            (qty_int, rate, company_name, name),
                        )
                    if cur is not None and cur.rowcount > 0:
                        updated_count += cur.rowcount
                    else:
                        unmatched += 1
        finally:
            conn.close()

        logging.info(
            f"[LocalDB] Stock balances refreshed for {company_name}: "
            f"updated={updated_count}, unmatched={unmatched}, unreadable={unreadable}, "
            f"tally_items={len(items)}"
        )
        return updated_count

    except Exception as e:
        logging.error(f"Stock item balance feed failed for {company_name}: {e}")
        return 0

def _fetch_all_stock_guids(company_name: str) -> set:
    """Lightweight catalog of every active stock item GUID in Tally."""
    xml_request = f"""
<ENVELOPE>
    <HEADER>
        <VERSION>1</VERSION>
        <TALLYREQUEST>Export</TALLYREQUEST>
        <TYPE>Collection</TYPE>
        <ID>All Stock Item GUIDs</ID>
    </HEADER>
    <BODY>
        <DESC>
            <STATICVARIABLES>
                <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
                <SVCURRENTCOMPANY>{escape(company_name)}</SVCURRENTCOMPANY>
            </STATICVARIABLES>
            <TDL>
                <TDLMESSAGE>
                    <COLLECTION NAME="All Stock Item GUIDs">
                        <TYPE>Stock Item</TYPE>
                        <FETCH>GUID, ISDELETED</FETCH>
                    </COLLECTION>
                </TDLMESSAGE>
            </TDL>
        </DESC>
    </BODY>
</ENVELOPE>
"""
    config = load_config()
    response = requests.post(
        config["tally_url"],
        data=xml_request.encode("utf-8"),
        headers={"Content-Type": "application/xml", "Connection": "close"},
        timeout=120,
    )
    response.raise_for_status()
    data = xmltodict.parse(clean_tally_xml(decode_response(response)))
    collection = data.get("ENVELOPE", {}).get("BODY", {}).get("DATA", {}).get("COLLECTION", {})
    items = collection.get("STOCKITEM", [])
    if isinstance(items, dict):
        items = [items]
    guids = set()
    for item in items:
        if get_text(item.get("ISDELETED")) == "Yes":
            continue
        g = get_text(item.get("GUID"))
        if g:
            guids.add(g)
    return guids


def get_stock_items(company_name: str = None):
    if not company_name:
        company_details = get_company_details()
        company_name = company_details.get("company_name", "Unknown") if company_details else "Unknown"

    from .local_db import get_max_alter_id_local, get_local_guids
    # FULL_RECONCILE=1 forces the watermark back to 0 for a complete re-read.
    max_alter_id = reconcile_from_id(get_max_alter_id_local("stock_items", company_name))
    # Backfill items missed on earlier syncs (incremental alter_id gaps).
    missing_count = 0
    try:
        tally_guids = _fetch_all_stock_guids(company_name)
        local_guids = get_local_guids("stock_items", company_name)
        if not local_guids:
            # Fresh/empty cache — must pull full master list before balances can apply
            missing_count = max(len(tally_guids), 1)
        else:
            missing_count = len(tally_guids - local_guids) if tally_guids else 0
    except Exception as e:
        logging.warning(f"Stock item GUID catalog check failed, using full fetch: {e}")
        missing_count = 1  # force full fetch when catalog unavailable

    use_incremental = max_alter_id > 0 and missing_count == 0
    filter_tag = "<FILTER>AlterIdFilter</FILTER>" if use_incremental else ""
    formula_tag = (
        f'<SYSTEM TYPE="Formulae" NAME="AlterIdFilter">$AlterId &gt; {max_alter_id}</SYSTEM>'
        if use_incremental else ""
    )
    if missing_count > 0:
        logging.info(
            f"[StockItems] {missing_count} stock item(s) in Tally not yet local for {company_name}; running full master fetch."
        )
    elif use_incremental:
        logging.info(f"[StockItems] Incremental master fetch (alter_id > {max_alter_id}) for {company_name}")
    today_str = datetime.now().strftime("%Y%m%d")

    xml_request = f"""
<ENVELOPE>
    <HEADER>
        <VERSION>1</VERSION>
        <TALLYREQUEST>Export</TALLYREQUEST>
        <TYPE>Collection</TYPE>
        <ID>Stock Item Collection</ID>
    </HEADER>
    <BODY>
        <DESC>
            <STATICVARIABLES>
                <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
                <SVCURRENTCOMPANY>{escape(company_name)}</SVCURRENTCOMPANY>
                <SVFROMDATE TYPE="Date">20000101</SVFROMDATE>
                <SVTODATE TYPE="Date">{today_str}</SVTODATE>
            </STATICVARIABLES>
            <TDL>
                <TDLMESSAGE>
                    <COLLECTION NAME="Stock Item Collection">
                        <TYPE>Stock Item</TYPE>
                        <FETCH>NAME, MASTERID, GUID, PARENT, BASEUNITS, ADDITIONALUNITS, PARTNO, PARTNUMBER, ITEMCODE, MAILINGNAME.LIST, CATEGORY, HSNCODE, GSTHSN, HSNDETAILS.LIST, DESCRIPTION, ADDITIONALDESCRIPTION, OPENINGBALANCE, OPENINGRATE, VATRATE, IGSTRATE, GSTDETAILS.LIST, MRP, MAXIMUMRETAILPRICE, MRPDETAILS.LIST, STANDARDCOST, STANDARDPRICE, OPENINGVALUE, MINIMUMSALESPRICE, DISCOUNT, ISDELETED, LANGUAGENAME.LIST, CLOSINGBALANCE, CLOSINGRATE, CLOSINGVALUE, ALTERID</FETCH>
                        {filter_tag}
                    </COLLECTION>
                    {formula_tag}
                </TDLMESSAGE>
            </TDL>
        </DESC>
    </BODY>
</ENVELOPE>
"""
    config = load_config()
    tally_url = config["tally_url"]
    
    response = requests.post(
        tally_url,
        data=xml_request.encode("utf-8"),
        headers={"Content-Type": "application/xml", "Connection": "close"},
        timeout=300
    )
    response.raise_for_status()
    response_text = clean_tally_xml(decode_response(response))

    try:
        data = xmltodict.parse(response_text)
    except Exception as e:
        # An unparseable master feed is a failure, not "no stock items changed".
        logging.error(f"Error parsing Tally stock item XML response: {e}")
        raise


    collection = data.get("ENVELOPE", {}).get("BODY", {}).get("DATA", {}).get("COLLECTION", {})
    stock_items = collection.get("STOCKITEM", [])
    
    if isinstance(stock_items, dict):
        stock_items = [stock_items]
        
    group_gst_rates = get_stock_group_gst_rates(company_name)
    
    all_stock_items = []
    
    for item in stock_items:
        try:
            item_name = get_text(item.get("@NAME") or item.get("NAME"))
            if not item_name:
                continue
                
            aliases = []
            language_list = item.get("LANGUAGENAME.LIST")
            if language_list:
                name_list = language_list.get("NAME.LIST")
                if isinstance(name_list, dict):
                    names = name_list.get("NAME", [])
                    if not isinstance(names, list):
                        names = [names]
                    aliases = [clean_text(x) for x in names if clean_text(x)]
            aliases = [a for a in aliases if a != item_name]
            
            item_parent = get_text(item.get("PARENT")) or None
            
            gst_rate = safe_float(item.get("VATRATE"), 0)
            if gst_rate == 0:
                gst_rate = extract_gst_rate(item)
            if gst_rate == 0 and item_parent:
                gst_rate = group_gst_rates.get(item_parent, 0.0)
            
            qty, rate = _qty_rate_from_closing(item)
            closing_value = safe_float(item.get("CLOSINGVALUE"), 0)
            # Master fetch may be incremental without closing fields; fall back to opening
            if qty == 0 and rate == 0:
                qty = _integer_quantity(safe_float(item.get("OPENINGBALANCE"), 0))
                oval = safe_float(item.get("OPENINGVALUE"), 0)
                orate = abs(safe_float(item.get("OPENINGRATE"), 0))
                if qty != 0 and oval != 0:
                    rate = round(abs(oval / qty), 6)
                else:
                    rate = round(orate, 6)
                if closing_value == 0:
                    closing_value = oval

            # Tally returns a dated history for HSN and MRP; take today's entry.
            hsn_detail = effective_dict(item.get("HSNDETAILS.LIST"))
            mrp = safe_float(effective_dict(item.get("MRPDETAILS.LIST")).get("MRPRATE"), 0)
            if not mrp:
                mrp = safe_float(item.get("MRP")) or safe_float(item.get("MAXIMUMRETAILPRICE"))

            row = {
                "company_name": company_name,
                "ItemName": item_name,
                "PartNumber": get_text(item.get("PARTNO")) or get_text(item.get("PARTNUMBER")) or get_text(item.get("ITEMCODE")) or get_text((item.get("MAILINGNAME.LIST") or {}).get("MAILINGNAME")),
                "ItemUnit": get_text(item.get("BASEUNITS")) or "Nos",
                "ItemAltUnit": get_text(item.get("ADDITIONALUNITS")) or None,
                "ItemParent": item_parent,
                "Category": get_text(item.get("CATEGORY")) or None,
                "Alias": aliases[0] if len(aliases) > 0 else None,
                "hsn": get_text(hsn_detail.get("HSNCODE")) or get_text(item.get("HSNCODE")) or get_text(item.get("GSTHSN")) or None,
                "Description": get_text(item.get("DESCRIPTION")) or get_text(item.get("ADDITIONALDESCRIPTION")) or None,
                "ItemQuantity": qty,
                "ItemRate": rate,
                "ClosingValue": -closing_value,
                "GstRate": gst_rate,
                "MRP": mrp,
                "StandardCost": safe_float(item.get("STANDARDCOST")),
                "StandardPrice": safe_float(item.get("STANDARDPRICE")),
                "OpeningValue": safe_float(item.get("OPENINGVALUE")),
                "floor_rate": safe_float(item.get("MINIMUMSALESPRICE")),
                "offer_discount_percentage": safe_float(item.get("DISCOUNT"), 0),
                "is_active": get_text(item.get("ISDELETED")) != "Yes",
                "guid": get_text(item.get("GUID")) or (
                    f"stock_{company_name}_{get_text(item.get('MASTERID'))}".replace(" ", "_")
                    if get_text(item.get("MASTERID")) else None
                ),
                "master_id": int(float(get_text(item.get("MASTERID")))) if get_text(item.get("MASTERID")) else None,
                "alter_id": int(safe_float(item.get("ALTERID"), 0)),
            }
            all_stock_items.append(row)
        except Exception as e:
            # Skip the unreadable item but keep the rest of the master fetch going.
            logging.error(
                f"Error parsing stock item '{get_text(item.get('@NAME') or item.get('NAME'))}': {e}"
            )

    if stock_items and not all_stock_items:
        raise RuntimeError(
            f"Tally returned {len(stock_items)} stock item(s) for {company_name} "
            "but none could be parsed"
        )

    return all_stock_items

def save_stock_items_to_supabase(stock_items, company_name: str = None):
    comp_name = company_name or (stock_items[0].get("company_name", "") if stock_items else "")

    from .local_db import save_to_local_db
    staged = 0
    if stock_items:
        staged = save_to_local_db("stock_items", stock_items, comp_name)
        logging.info(f"[LocalDB] Staged {staged} stock items locally in sync_cache.db")
    # Always re-apply today's closing qty/rate after master upsert (authoritative)
    if comp_name:
        try:
            bal = sync_stock_item_balances(comp_name)
            logging.info(f"[StockItems] Post-save balance refresh wrote {bal} row(s) for {comp_name}")
        except Exception as e:
            logging.error(f"Post-save stock balance refresh failed for {comp_name}: {e}")
    return staged


def reconcile_deleted_stock_items(company_name: str) -> int:
    """
    Fetches all active Stock Item GUIDs from Tally, compares them against
    Supabase 'stock_items', and deletes any items that were deleted in Tally.
    """
    config = load_config()
    tally_url = config.get("tally_url", "http://localhost:9000")

    xml_request = f"""<ENVELOPE>
    <HEADER>
        <VERSION>1</VERSION>
        <TALLYREQUEST>Export</TALLYREQUEST>
        <TYPE>Collection</TYPE>
        <ID>AllStockItemGuids</ID>
    </HEADER>
    <BODY>
        <DESC>
            <STATICVARIABLES>
                <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
                <SVCURRENTCOMPANY>{escape(company_name)}</SVCURRENTCOMPANY>
            </STATICVARIABLES>
            <TDL>
                <TDLMESSAGE>
                    <COLLECTION NAME="AllStockItemGuids">
                        <TYPE>Stock Item</TYPE>
                        <FETCH>GUID,ISDELETED</FETCH>
                    </COLLECTION>
                </TDLMESSAGE>
            </TDL>
        </DESC>
    </BODY>
</ENVELOPE>"""

    try:
        response = requests.post(
            tally_url,
            data=xml_request.encode("utf-8"),
            headers={"Content-Type": "application/xml"},
            timeout=120
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
        items = collection.get("STOCKITEM", [])
        if isinstance(items, dict):
            items = [items]

        tally_active_guids = set()
        for item in items:
            if not isinstance(item, dict):
                continue
            if get_text(item.get("ISDELETED")) == "Yes":
                continue
            g = get_text(item.get("GUID"))
            if g:
                tally_active_guids.add(g)

    except Exception as e:
        # Returning 0 is the safe outcome, but it is a failure and not "nothing deleted".
        logging.error(
            f"Could not fetch active Stock Item GUIDs from Tally for {company_name}; "
            f"deletion reconciliation skipped this cycle: {e}"
        )
        return 0

    from .local_db import reconcile_local_deletions
    return reconcile_local_deletions("stock_items", tally_active_guids, company_name)




# =====================================================
# MAIN
# =====================================================

if __name__ == "__main__":

    try:

        logging.info("Fetching stock items from Tally...")

        stock_items = get_stock_items()

        logging.info(f"Found {len(stock_items)} stock items.")

        logging.info("Saving to Supabase...")

        save_stock_items_to_supabase(stock_items)

        logging.info(
            f"\nStock Items synced successfully ({len(stock_items)} records)."
        )

    except Exception as e:

        logging.info(f"Stock Item sync failed: {e}")

        raise