from .source_values import clean_source_xml, decode_response, reconcile_from_id
import logging
import re
import requests
import xmltodict
from datetime import datetime, timezone
from xml.sax.saxutils import escape
from supabase import create_client
from .config import load_config
from .tally_companies import get_company_details

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

def clean_tally_xml(xml_text):
    if not xml_text:
        return ""
    return clean_source_xml(xml_text)

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

def get_godowns(company_name: str = None):
    if not company_name:
        company_details = get_company_details()
        company_name = company_details.get("company_name", "Unknown") if company_details else "Unknown"

    from .local_db import get_max_alter_id_local
    max_alter_id = reconcile_from_id(get_max_alter_id_local("tally_godowns", company_name))
    filter_tag = "<FILTER>AlterIdFilter</FILTER>" if max_alter_id > 0 else ""
    formula_tag = f'<SYSTEM TYPE="Formulae" NAME="AlterIdFilter">$AlterId &gt; {max_alter_id}</SYSTEM>' if max_alter_id > 0 else ""

    xml_request = f"""
<ENVELOPE>
    <HEADER>
        <VERSION>1</VERSION>
        <TALLYREQUEST>Export</TALLYREQUEST>
        <TYPE>Collection</TYPE>
        <ID>Godown Collection</ID>
    </HEADER>
    <BODY>
        <DESC>
            <STATICVARIABLES>
                <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
                <SVCURRENTCOMPANY>{escape(company_name)}</SVCURRENTCOMPANY>
            </STATICVARIABLES>
            <TDL>
                <TDLMESSAGE>
                    <COLLECTION NAME="Godown Collection">
                        <TYPE>Godown</TYPE>
                        <NATIVEMETHOD>*</NATIVEMETHOD>
                        <FETCH>NAME, MASTERID, GUID, PARENT, ADDRESS, ALTERID</FETCH>
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

    try:
        response = requests.post(
            tally_url,
            data=xml_request.encode("utf-8"),
            headers={"Content-Type": "application/xml", "Connection": "close"},
            timeout=120
        )
        response.raise_for_status()
    except Exception as e:
        logging.error(f"Error fetching Godowns from Tally: {e}")
        raise

    try:
        clean_xml = clean_tally_xml(decode_response(response))
        clean_xml = clean_xml.replace('encoding="utf-16"', 'encoding="utf-8"')
        parsed_data = xmltodict.parse(clean_xml)
        collection = parsed_data.get("ENVELOPE", {}).get("BODY", {}).get("DATA", {}).get("COLLECTION", {}).get("GODOWN", [])
    except Exception as e:
        logging.error(f"Error parsing XML for Godowns: {e}")
        raise

    if not isinstance(collection, list):
        collection = [collection]

    godowns_list = []
    for item in collection:
        try:
            if not item or not isinstance(item, dict):
                continue
            name = get_text(item.get("@NAME") or item.get("NAME"))
            if not name:
                continue

            master_id_raw = get_text(item.get("MASTERID"))
            master_id = str(int(float(master_id_raw))) if master_id_raw else None

            row = {
                "name": name,
                "parent": get_text(item.get("PARENT")) or None,
                "address": get_text(item.get("ADDRESS")) or None,
                "company_name": company_name,
                "guid": get_text(item.get("GUID")),
                "master_id": master_id,
                "alter_id": int(float(get_text(item.get("ALTERID")) or 0)),
                "is_active": True,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            godowns_list.append(row)
        except Exception as e:
            logging.error(f"Godown Error: {e}")

    return godowns_list

def save_godowns_to_supabase(godowns, company_name: str = None):
    if not godowns:
        return 0

    comp_name = company_name or (godowns[0].get("company_name", "") if godowns else "")

    from .local_db import save_to_local_db
    staged = save_to_local_db("tally_godowns", godowns, comp_name)
    logging.info(f"[LocalDB] Staged {staged} godowns locally in sync_cache.db")
    return staged


def reconcile_deleted_godowns(company_name: str) -> int:
    """
    Fetches all active Godown GUIDs from Tally, compares them against
    Supabase 'tally_godowns', and deletes any godowns that were deleted in Tally.
    """
    config = load_config()
    tally_url = config.get("tally_url", "http://localhost:9000")

    xml_request = f"""<ENVELOPE>
    <HEADER>
        <VERSION>1</VERSION>
        <TALLYREQUEST>Export</TALLYREQUEST>
        <TYPE>Collection</TYPE>
        <ID>AllGodownGuids</ID>
    </HEADER>
    <BODY>
        <DESC>
            <STATICVARIABLES>
                <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
                <SVCURRENTCOMPANY>{escape(company_name)}</SVCURRENTCOMPANY>
            </STATICVARIABLES>
            <TDL>
                <TDLMESSAGE>
                    <COLLECTION NAME="AllGodownGuids">
                        <TYPE>Godown</TYPE>
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
        clean_xml = clean_tally_xml(decode_response(response))
        data = xmltodict.parse(clean_xml)
        collection = (
            data.get("ENVELOPE", {})
            .get("BODY", {})
            .get("DATA", {})
            .get("COLLECTION", {})
        )
        items = collection.get("GODOWN", [])
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
        logging.error(f"Could not fetch active Godown GUIDs from Tally for reconciliation: {e}")
        raise

    from .local_db import reconcile_local_deletions
    return reconcile_local_deletions("tally_godowns", tally_active_guids, company_name)


