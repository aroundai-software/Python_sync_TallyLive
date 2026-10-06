import logging
import re
import requests
import time
import xmltodict
from datetime import datetime, timezone
import concurrent.futures
from xml.sax.saxutils import escape

from supabase import create_client

from .config import load_config
from .tally_companies import get_company_details
from .date_utils import get_current_fy_item_date_ranges, get_current_fy_monthly_date_ranges
from .local_db import stage_records, get_pending_records, mark_as_synced, mark_as_failed

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

def get_text_or_none(value):
    text = get_text(value)
    return text if text else None

def safe_float(value, default=0):
    try:
        value = get_text(value)
        value = value.replace(",", "")
        match = re.search(r"-?\d+(\.\d+)?", value)
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
        return datetime.strptime(value, "%Y%m%d").date().isoformat()
    except Exception:
        return None

def clean_tally_xml(xml_text):
    xml_text = re.sub(r'&#(?:x[0-9A-Fa-f]+|\d+);', '', xml_text)
    xml_text = re.sub(r"&(?!amp;|lt;|gt;|quot;|apos;)", "&amp;", xml_text)
    return xml_text

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


def as_list(value):
    if not value:
        return []
    if isinstance(value, list):
        return value
    return [value]


def get_ledger_entries(voucher):
    entries = (
        voucher.get("ALLLEDGERENTRIES.LIST")
        or voucher.get("LEDGERENTRIES.LIST")
        or []
    )
    return [entry for entry in as_list(entries) if isinstance(entry, dict)]


def signed_entry_amount(entry):
    """
    Tally credits are positive / ISDEEMEDPOSITIVE=No.
    Debits are negative / ISDEEMEDPOSITIVE=Yes.
    Prefer ISDEEMEDPOSITIVE so a missing minus sign still classifies correctly.
    """
    raw = safe_float(entry.get("AMOUNT"))
    deemed = get_text(entry.get("ISDEEMEDPOSITIVE")).lower()
    if deemed == "yes":
        return -abs(raw)
    if deemed == "no":
        return abs(raw)
    return raw


def party_net_amount(entries, ledger_name):
    """
    Receipts can repeat the same party once per bill allocation.
    Voucher-level AMOUNT then only contains the first allocation.
    Sum every matching party line so the ledger voucher matches Tally.
    """
    if not ledger_name:
        return 0.0, False

    total = 0.0
    matched = False
    for entry in entries:
        if get_text(entry.get("LEDGERNAME")) == ledger_name:
            total += signed_entry_amount(entry)
            matched = True
    return total, matched

# =====================================================
# TALLY FETCH
# =====================================================

def get_daybook(company_name: str = None):
    config = load_config()
    tally_url = config["tally_url"]
    if not company_name:
        company_details = get_company_details()
        company_name = company_details.get("company_name", "")
    now = datetime.now(timezone.utc).isoformat()
    
    date_ranges = get_current_fy_monthly_date_ranges()
    
    logging.info(f"Fetching daybook in {len(date_ranges)} monthly chunks...")
    
    daybook_list = []
    
    from .local_db import get_max_alter_id_local
    max_alter_id = get_max_alter_id_local("tally_daybook", company_name)
    logging.info(f"Daybook incremental sync from alter_id > {max_alter_id}")
    
    total_chunks = len(date_ranges)
    for chunk_idx, (start_date, end_date) in enumerate(date_ranges, 1):
        logging.info(f"[{chunk_idx}/{total_chunks}] Fetching Daybook {start_date} to {end_date}...")
        chunk_entries = []
        
        alter_filter_tag = "<FILTER>AlterIdFilter</FILTER>" if max_alter_id > 0 else ""
        alter_formula_tag = f'<SYSTEM TYPE="Formulae" NAME="AlterIdFilter">$AlterId &gt; {max_alter_id}</SYSTEM>' if max_alter_id > 0 else ""

        xml_request = f"""
<ENVELOPE>
    <HEADER>
        <VERSION>1</VERSION>
        <TALLYREQUEST>Export</TALLYREQUEST>
        <TYPE>Collection</TYPE>
        <ID>Daybook Collection</ID>
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
                    <COLLECTION NAME="Daybook Collection">
                        <TYPE>Voucher</TYPE>
                        <FETCH>
                            DATE,
                            GUID,
                            MASTERID,
                            VOUCHERNUMBER,
                            VOUCHERTYPENAME,
                            PARTYLEDGERNAME,
                            PARTYNAME,
                            AMOUNT,
                            NARRATION,
                            ISDELETED,
                            ISCANCELLED,
                            ALLLEDGERENTRIES.LIST,
                            LEDGERENTRIES.LIST,
                            ALTERID
                        </FETCH>
                        {alter_filter_tag}
                    </COLLECTION>
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
            
            envelope = data.get("ENVELOPE") or {} if isinstance(data, dict) else {}
            body = envelope.get("BODY") or {} if isinstance(envelope, dict) else {}
            b_data = body.get("DATA") or {} if isinstance(body, dict) else {}
            collection = b_data.get("COLLECTION") or {} if isinstance(b_data, dict) else {}

            vouchers = collection.get("VOUCHER", []) if isinstance(collection, dict) else []
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

                guid = get_text(voucher.get("GUID"))
                master_id = int(safe_float(voucher.get("MASTERID")))
                date = format_tally_date(voucher.get("DATE"))
                
                if not date:
                    continue

                voucher_number = get_text(voucher.get("VOUCHERNUMBER"))
                voucher_type = get_text(voucher.get("VOUCHERTYPENAME"))
                
                party_ledger_name = get_text(voucher.get("PARTYLEDGERNAME"))
                party_name = get_text(voucher.get("PARTYNAME"))
                primary_ledger_name = party_ledger_name if party_ledger_name else party_name
                
                narration = get_text_or_none(voucher.get("NARRATION"))

                ledger_entries = get_ledger_entries(voucher)

                if not ledger_entries:
                    raw_amount = safe_float(voucher.get("AMOUNT"))
                    row = {
                        "date": date,
                        "voucher_number": voucher_number if voucher_number else None,
                        "voucher_type": voucher_type,
                        "ledger_name": primary_ledger_name if primary_ledger_name else None,
                        "particulars": None,
                        "amount": abs(raw_amount),
                        "is_debit": raw_amount < 0,
                        "narration": narration,
                        "company_name": company_name,
                        "guid": guid,
                        "master_id": str(master_id) if master_id else None,
                        "alter_id": int(safe_float(voucher.get("ALTERID"), 0)),
                        "updated_at": now,
                    }
                    daybook_list.append(row)
                else:
                    # Group entries by ledger name
                    grouped_ledgers = {}
                    for entry in ledger_entries:
                        lname = get_text(entry.get("LEDGERNAME"))
                        if not lname:
                            continue
                        if lname not in grouped_ledgers:
                            grouped_ledgers[lname] = []
                        grouped_ledgers[lname].append(entry)

                    # Determine primary ledger name
                    if not primary_ledger_name or primary_ledger_name not in grouped_ledgers:
                        primary_ledger_name = next(iter(grouped_ledgers.keys()), None)

                    # Compute signed net amount for each ledger
                    ledger_summaries = {}
                    for lname, entries in grouped_ledgers.items():
                        net_signed = sum(signed_entry_amount(e) for e in entries)
                        if abs(net_signed) < 0.001:
                            continue
                        ledger_summaries[lname] = {
                            "amount": abs(net_signed),
                            "is_debit": net_signed < 0,
                        }

                    # If ledger_summaries is empty (rare), fall back to raw amount
                    if not ledger_summaries:
                        raw_amount = safe_float(voucher.get("AMOUNT"))
                        ledger_summaries[primary_ledger_name or "Unknown"] = {
                            "amount": abs(raw_amount),
                            "is_debit": raw_amount < 0,
                        }

                    for lname, info in ledger_summaries.items():
                        # Find opposing ledgers (different debit/credit side)
                        opposing = [
                            other_name for other_name, other_info in ledger_summaries.items()
                            if other_info["is_debit"] != info["is_debit"]
                        ]
                        if len(opposing) == 1:
                            particulars = opposing[0]
                        elif len(opposing) > 1:
                            if primary_ledger_name in opposing and primary_ledger_name != lname:
                                particulars = primary_ledger_name
                            else:
                                particulars = "As per details"
                        else:
                            particulars = None

                        # Primary entry uses base voucher guid; sub-entries use slugged sub-guid
                        if lname == primary_ledger_name:
                            entry_guid = guid
                        else:
                            clean_lname = re.sub(r'[^a-zA-Z0-9]', '_', lname)
                            entry_guid = f"{guid}#sub#{clean_lname}"

                        row = {
                            "date": date,
                            "voucher_number": voucher_number if voucher_number else None,
                            "voucher_type": voucher_type,
                            "ledger_name": lname,
                            "particulars": particulars,
                            "amount": info["amount"],
                            "is_debit": info["is_debit"],
                            "narration": narration,
                            "company_name": company_name,
                            "guid": entry_guid,
                            "master_id": str(master_id) if master_id else None,
                            "alter_id": int(safe_float(voucher.get("ALTERID"), 0)),
                            "updated_at": now,
                        }
                        daybook_list.append(row)
                        chunk_entries.append(row)
                
            if cancelled_guids:
                from .local_db import stage_deletions
                stage_deletions("tally_daybook", cancelled_guids, company_name)
                logging.info(f"Staged {len(cancelled_guids)} cancelled vouchers for daybook deletion")

            if chunk_entries:
                stage_records("daybook", chunk_entries, company_name)
                logging.info(f"[{chunk_idx}/{total_chunks}] Staged {len(chunk_entries)} daybook entries locally ({start_date}-{end_date})")
            else:
                logging.info(f"[{chunk_idx}/{total_chunks}] 0 new daybook entries in ({start_date}-{end_date})")

        except Exception as e:
            logging.info(f"Chunk Error Daybook ({start_date}-{end_date}): {e}")
            
        # Breathing gap for Tally single-threaded event loop
        time.sleep(0.5)

    unique_entries = {}
    for entry in daybook_list:
        guid = entry.get("guid")
        if guid:
            unique_entries[guid] = entry

    return list(unique_entries.values())


# =====================================================
# SUPABASE SAVE
# =====================================================

def save_daybook_to_supabase(daybook_entries=None, company_name=None):
    if not daybook_entries:
        return 0

    comp_name = company_name or (daybook_entries[0].get("company_name", "") if daybook_entries else "")

    from .local_db import save_to_local_db
    staged = save_to_local_db("tally_daybook", daybook_entries, comp_name)
    logging.info(f"[LocalDB] Staged {staged} daybook entries locally in sync_cache.db")
    return staged


def reconcile_deleted_daybook(company_name: str) -> int:
    """
    Fetches all active Voucher GUIDs from Tally for the current FY,
    compares them against Supabase 'tally_daybook', and deletes any entries
    that were deleted in Tally.
    """
    config = load_config()
    tally_url = config.get("tally_url", "http://localhost:9000")
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
        <ID>AllDaybookGuids</ID>
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
                    <COLLECTION NAME="AllDaybookGuids">
                        <TYPE>Voucher</TYPE>
                        <FETCH>GUID,ISDELETED,ISCANCELLED</FETCH>
                        <FILTER>DateFilter</FILTER>
                    </COLLECTION>
                    <SYSTEM TYPE="Formulae" NAME="DateFilter">$Date &gt;= $$Date:"{fy_start}" AND $Date &lt;= $$Date:"{fy_end}"</SYSTEM>
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
        logging.warning(f"Could not fetch active Daybook GUIDs from Tally for reconciliation: {e}")
        return 0

    if not tally_active_guids:
        logging.warning("Tally returned 0 active Daybook GUIDs; skipping reconciliation to prevent mass-deletion.")
        return 0

    from .local_db import reconcile_local_deletions
    return reconcile_local_deletions("tally_daybook", tally_active_guids, company_name)



# =====================================================
# MAIN
# =====================================================

if __name__ == "__main__":
    try:
        logging.info("Fetching daybook from Tally...")
        daybook_data = get_daybook()
        logging.info(f"Found {len(daybook_data)} daybook entries.")
        logging.info("Saving to Supabase...")
        save_daybook_to_supabase(daybook_data)
        logging.info(f"\nDaybook synced successfully ({len(daybook_data)} records).")
    except Exception as e:
        logging.info(f"Daybook sync failed: {e}")
        raise
