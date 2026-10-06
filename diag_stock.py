"""Diagnostic: compare Tally stock items vs Supabase for one company."""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.stock_items import get_text, clean_tally_xml, safe_float
from src.config import load_config
import requests
import xmltodict
from xml.sax.saxutils import escape
from supabase import create_client

COMPANY = "SIDHARTH IT SOLUTIONS 26-27"


def fetch_all_tally_stock(company_name: str):
    """Fetch ALL stock items from Tally without alter_id filter."""
    today_str = __import__("datetime").datetime.now().strftime("%Y%m%d")
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
                        <FETCH>NAME, GUID, ISDELETED, ALTERID, CLOSINGBALANCE, CLOSINGVALUE</FETCH>
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
    data = xmltodict.parse(clean_tally_xml(response.text))
    collection = data.get("ENVELOPE", {}).get("BODY", {}).get("DATA", {}).get("COLLECTION", {})
    items = collection.get("STOCKITEM", [])
    if isinstance(items, dict):
        items = [items]
    return items


def fetch_reconcile_guids(company_name: str):
    """Same query as reconcile_deleted_stock_items."""
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
                        <TYPE>StockItem</TYPE>
                        <FETCH>GUID,ISDELETED</FETCH>
                    </COLLECTION>
                </TDLMESSAGE>
            </TDL>
        </DESC>
    </BODY>
</ENVELOPE>"""
    config = load_config()
    response = requests.post(
        config["tally_url"],
        data=xml_request.encode("utf-8"),
        headers={"Content-Type": "application/xml"},
        timeout=120,
    )
    response.raise_for_status()
    data = xmltodict.parse(clean_tally_xml(response.text))
    collection = data.get("ENVELOPE", {}).get("BODY", {}).get("DATA", {}).get("COLLECTION", {})
    items = collection.get("STOCKITEM", [])
    if isinstance(items, dict):
        items = [items]
    return items


def main():
    config = load_config()
    sb = create_client(config["supabase_url"], config["supabase_key"])
    sb_rows = (
        sb.table("stock_items")
        .select("ItemName,guid,is_active")
        .eq("company_name", COMPANY)
        .execute()
        .data
    )
    sb_names = {r["ItemName"] for r in sb_rows}
    sb_guids = {r["guid"] for r in sb_rows if r.get("guid")}

    print(f"Supabase: {len(sb_rows)} items")

    try:
        tally_items = fetch_all_tally_stock(COMPANY)
    except Exception as e:
        print(f"Tally full fetch failed: {e}")
        tally_items = []

    active = [i for i in tally_items if get_text(i.get("ISDELETED")) != "Yes"]
    print(f"Tally (Stock Item type, no filter): {len(tally_items)} total, {len(active)} active")

    tally_by_name = {}
    no_guid = []
    for it in active:
        name = get_text(it.get("@NAME"))
        guid = get_text(it.get("GUID"))
        if not guid:
            no_guid.append(name)
        tally_by_name[name] = guid

    missing_in_sb = sorted(set(tally_by_name) - sb_names)
    extra_in_sb = sorted(sb_names - set(tally_by_name))

    print("\n--- Missing in Supabase (in Tally, not in app) ---")
    for n in missing_in_sb:
        print(f"  {repr(n)}  guid={tally_by_name.get(n)}")

    print("\n--- Extra in Supabase (in app, not in Tally full fetch) ---")
    for n in extra_in_sb:
        print(f"  {repr(n)}")

    if no_guid:
        print(f"\n--- Tally items with NO GUID ({len(no_guid)}) ---")
        for n in no_guid:
            print(f"  {repr(n)}")

    try:
        recon_items = fetch_reconcile_guids(COMPANY)
        recon_active = [i for i in recon_items if get_text(i.get("ISDELETED")) != "Yes"]
        recon_guids = {get_text(i.get("GUID")) for i in recon_active if get_text(i.get("GUID"))}
        print(f"\nReconcile query (StockItem type): {len(recon_items)} total, {len(recon_active)} active, {len(recon_guids)} guids")
        wrongly_deleted = sb_guids - recon_guids
        if wrongly_deleted:
            print(f"  Items in Supabase but NOT in reconcile guids ({len(wrongly_deleted)}):")
            for r in sb_rows:
                if r.get("guid") in wrongly_deleted:
                    print(f"    {r['ItemName']}")
    except Exception as e:
        print(f"Reconcile fetch failed: {e}")

    # Test incremental filter
    from src.local_db import get_max_alter_id_local
    max_id = get_max_alter_id_local("stock_items", COMPANY)
    print(f"\nLocal max alter_id: {max_id}")
    skipped = [it for it in active if safe_float(it.get("ALTERID"), 0) <= max_id]
    skipped_names = sorted(get_text(it.get("@NAME")) for it in skipped)
    missing_and_skipped = [n for n in missing_in_sb if n in skipped_names]
    if missing_and_skipped:
        print(f"Missing items would be SKIPPED by AlterIdFilter ({max_id}):")
        for n in missing_and_skipped:
            print(f"  {repr(n)}")


if __name__ == "__main__":
    main()
