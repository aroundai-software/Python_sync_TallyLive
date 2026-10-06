import logging
import re
import requests
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from xml.sax.saxutils import escape

from supabase import create_client

from .config import load_config
from .tally_companies import get_company_details


# =====================================================
# HELPERS
# =====================================================

def safe_text(parent, tag):

    node = parent.find(tag)

    if node is None:
        return None

    value = (node.text or "").strip()

    return value if value else None


def clean_tally_xml(xml_text):

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


def parse_opening_balance(raw):
    """
    Tally returns opening balance with a sign suffix:
      e.g. '15000.00 Dr' or '5000.00 Cr' or '0.00'
    Returns (amount: float, type: 'DR'|'CR'|None)
    """

    if not raw:
        return 0.0, None

    raw = raw.strip()
    ob_type = None

    if raw.lower().endswith("dr"):
        ob_type = "DR"
        raw = raw[:-2].strip()

    elif raw.lower().endswith("cr"):
        ob_type = "CR"
        raw = raw[:-2].strip()

    try:
        return float(raw), ob_type
    except ValueError:
        return 0.0, ob_type


def parse_amount(raw):
    """Parse a plain numeric amount from Tally (no Dr/Cr suffix)."""

    if not raw:
        return None

    raw = raw.strip()

    try:
        return float(raw)
    except ValueError:
        return None


def parse_signed_balance(raw):
    """
    Parses a balance string from Tally into a signed float.
    In Tally XML:
      - Negative value '-732117.00' or 'Dr' suffix = DEBIT (+732117.00 in DB)
      - Positive value '732117.00' or 'Cr' suffix = CREDIT (-732117.00 in DB)
    """
    if not raw:
        return 0.0

    raw = str(raw).strip()

    if raw.lower().endswith("dr"):
        return abs(parse_amount(raw[:-2]) or 0.0)
    elif raw.lower().endswith("cr"):
        return -abs(parse_amount(raw[:-2]) or 0.0)

    val = parse_amount(raw)
    if val is None:
        return 0.0

    if raw.startswith("-"):
        return abs(val)
    elif val > 0:
        return -abs(val)
    
    return 0.0


# =====================================================
# LEDGERS
# =====================================================

def extract_ledger_data(ledger, company_name):
    """
    Extract all relevant fields from a <LEDGER> XML element.
    Address lines come from LEDMAILINGDETAILS.LIST > ADDRESS.LIST > ADDRESS (multi-line).
    TRN from LEDGSTREGDETAILS.LIST > GSTIN.
    """

    # --- Basic identity ---
    name = ledger.attrib.get("NAME", "").strip()
    guid = safe_text(ledger, "GUID") or ledger.attrib.get("GUID", "").strip()

    master_id_raw = (
        safe_text(ledger, "MASTERID")
        or ledger.attrib.get("MASTERID")
    )

    try:
        master_id = int(str(master_id_raw).strip())
    except (ValueError, AttributeError, TypeError):
        master_id = 0

    try:
        alter_id = int(str(safe_text(ledger, "ALTERID") or "0").strip())
    except (ValueError, AttributeError, TypeError):
        alter_id = 0

    ledger_group = safe_text(ledger, "PARENT") or ""

    alias = None

    language_list = ledger.find("LANGUAGENAME.LIST")

    if language_list is not None:

        names = []

        for name_node in language_list.findall(".//NAME"):
            value = (name_node.text or "").strip()
            if value:
                names.append(value)

        if len(names) > 1:
            alias = names[1]

    if not alias:
        alias = safe_text(ledger, "BASICALIASNAME")

    if not alias:
        alias = safe_text(ledger, "ALTERNATENAME")

    # --- Mailing / address details ---
    address_line1 = address_line2 = address_line3 = None
    city = emirate = country = pin_code = None

    mailing = ledger.find("LEDMAILINGDETAILS.LIST")
    if mailing is None:
        mailing = ledger

    if mailing is not None:

        addr_list = mailing.find("ADDRESS.LIST")

        if addr_list is not None:

            addresses = [
                (a.text or "").strip()
                for a in addr_list.findall("ADDRESS")
                if (a.text or "").strip()
            ]

            if len(addresses) >= 1:
                address_line1 = addresses[0]
            if len(addresses) >= 2:
                address_line2 = addresses[1]
            if len(addresses) >= 3:
                address_line3 = addresses[2]

        state_node = mailing.find("STATE")
        emirate = (
            (state_node.text or "").strip()
            if state_node is not None
            else None
        )

        country_node = mailing.find("COUNTRY")
        country = (
            (country_node.text or "").strip()
            if country_node is not None
            else None
        )

        pin_code = safe_text(mailing, "PINCODE")
        city = (
            safe_text(mailing, "CITY")
            or safe_text(mailing, "PLACEOFSUPPLY")
            or None
        )

    # --- Contact details ---
    mobile_number = (
        safe_text(ledger, "MOBILE")
        or safe_text(ledger, "MOBILENO")
        or safe_text(ledger, "LEDGERMOBILE")
        or safe_text(ledger, "PARTYMOBILENO")
    )

    phone_number = (
        safe_text(ledger, "PHONENUMBER")
        or safe_text(ledger, "LEDGERPHONE")
        or safe_text(ledger, "PARTYPHONENO")
    )

    email = (
        safe_text(ledger, "EMAIL")
        or safe_text(ledger, "PARTYEMAIL")
    )

    contact_person = (
        safe_text(ledger, "CONTACTPERSON")
        or safe_text(ledger, "CONTACT")
    )

    # --- GST / TRN ---
    trn_number = None
    gst_details = ledger.find("LEDGSTREGDETAILS.LIST")

    if gst_details is not None:
        trn_number = safe_text(gst_details, "GSTIN")

    # --- Financial ---
    ob_raw = safe_text(ledger, "OPENINGBALANCE")
    opening_balance, opening_balance_type = parse_opening_balance(ob_raw)

    credit_limit = parse_amount(safe_text(ledger, "CREDITLIMIT"))

    credit_period_raw = safe_text(ledger, "CREDITPERIOD")

    try:
        credit_period = int(credit_period_raw) if credit_period_raw else None
    except ValueError:
        credit_period = None

    ledger_code = (
        safe_text(ledger, "LEDGERCODE")
        or safe_text(ledger, "ALTERID")
    )

    return {

        "company_name":
            company_name,

        "guid":
            guid,

        "master_id":
            str(master_id) if master_id else None,
            
        "alter_id":
            alter_id,

        "ledger_name":
            name,

        "alias":
            alias,

        "ledger_group":
            ledger_group,

        "address_line1":
            address_line1,

        "address_line2":
            address_line2,

        "address_line3":
            address_line3,

        "city":
            city,

        "emirate":
            emirate or None,

        "country":
            country or "UAE",

        "pin_code":
            pin_code,

        "mobile_number":
            mobile_number,

        "phone_number":
            phone_number,

        "email":
            email,

        "contact_person":
            contact_person,

        "trn_number":
            trn_number,

        "trade_license_number":
            None,

        "opening_balance":
            round(opening_balance, 2),

        "opening_balance_type":
            opening_balance_type,

        "credit_limit":
            credit_limit,

        "credit_period":
            credit_period,

        "ledger_code":
            ledger_code,

        "area":
            None,

        "mobile":
            mobile_number,

        "phone":
            phone_number,

        "outstanding_balance":
            None,

        "current_balance":
            None,

        "status":
            "active",

        "transportation_cost":
            None,

        "sales_person":
            None,

        "credit_date":
            None,

        "is_active":
            True,

        "updated_at":
            datetime.now(
                timezone.utc
            ).isoformat(),
    }


def get_max_alter_id(table_name: str, company_name: str) -> int:
    from .local_db import get_max_alter_id_local
    return get_max_alter_id_local("customers", company_name)

def get_ledgers(company_name: str = None):
    if not company_name:
        company_details = get_company_details()
        company_name = company_details.get("company_name", "")

    max_alter_id = get_max_alter_id("customers", company_name)
    filter_tag = "<FILTER>AlterIdFilter</FILTER>" if max_alter_id > 0 else ""
    formula_tag = f'<SYSTEM TYPE="Formulae" NAME="AlterIdFilter">$AlterId &gt; {max_alter_id}</SYSTEM>' if max_alter_id > 0 else ""

    xml_request = f"""
<ENVELOPE>
    <HEADER>
        <VERSION>1</VERSION>
        <TALLYREQUEST>Export</TALLYREQUEST>
        <TYPE>Collection</TYPE>
        <ID>Ledger Collection</ID>
    </HEADER>
    <BODY>
        <DESC>
            <STATICVARIABLES>
                <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
                <SVCURRENTCOMPANY>{escape(company_name)}</SVCURRENTCOMPANY>
            </STATICVARIABLES>
            <TDL>
                <TDLMESSAGE>
                    <COLLECTION NAME="Ledger Collection">
                        <TYPE>Ledger</TYPE>
                        <FETCH>NAME, PARENT, OPENINGBALANCE, CLOSINGBALANCE, MAILINGNAME.LIST, ADDRESS.LIST, STATE, COUNTRY, PINCODE, CITY, PLACEOFSUPPLY, MOBILE, MOBILENO, LEDGERMOBILE, PARTYMOBILENO, PHONENUMBER, LEDGERPHONE, PARTYPHONENO, EMAIL, PARTYEMAIL, CONTACTPERSON, CONTACT, LEDGSTREGDETAILS.LIST, CREDITLIMIT, CREDITPERIOD, LEDGERCODE, ALTERID, MASTERID, GUID</FETCH>
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

    response_text = clean_tally_xml(response.text)
    root = ET.fromstring(response_text)

    ledgers = []
    for ledger in root.findall(".//COLLECTION/LEDGER"):
        name = ledger.attrib.get("NAME", "").strip()
        if not name:
            continue
        data = extract_ledger_data(ledger, company_name)
        ledgers.append(data)

    return ledgers

def save_ledgers_to_supabase(ledgers):

    config = load_config()

    supabase = create_client(
        config["supabase_url"],
        config["supabase_key"]
    )

    if not ledgers:
        logging.info("No ledgers to save.")
        return

    BATCH_SIZE = 100
    total_saved = 0
    batches = [ledgers[i: i + BATCH_SIZE] for i in range(0, len(ledgers), BATCH_SIZE)]

    import concurrent.futures

    def upload_batch(batch):
        try:
            supabase.table("ledgers").upsert(
                batch,
                on_conflict="company_name,guid"
            ).execute()
            return len(batch)
        except Exception as e:
            logging.info(f"Batch upload failed: {e}")
            return 0

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        results = executor.map(upload_batch, batches)
        total_saved = sum(results)

    return total_saved


# =====================================================
# CUSTOMERS (Debtors + Creditors)
# =====================================================

# Groups treated as debtors (receivable)
DEBTOR_GROUPS = [
    "sundry debtors",
    "debtors",
    "customers",
    "accounts receivable",
]

# Groups treated as creditors (payable)
CREDITOR_GROUPS = [
    "sundry creditors",
    "creditors",
    "suppliers",
    "accounts payable",
]

ALL_CUSTOMER_GROUPS = DEBTOR_GROUPS + CREDITOR_GROUPS


def extract_customer_data(ledger, company_name):
    """
    Extract customer/supplier data from a <LEDGER> XML element.
    ledger_type stores the parent group name (e.g. 'Sundry Debtors').
    """

    # --- Basic identity ---
    customer_name = ledger.attrib.get("NAME", "").strip()
    guid = safe_text(ledger, "GUID") or ledger.attrib.get("GUID", "").strip()

    master_id_raw = (
        safe_text(ledger, "MASTERID")
        or ledger.attrib.get("MASTERID")
    )

    try:
        master_id = str(int(str(master_id_raw).strip())) if master_id_raw else None
    except (ValueError, AttributeError, TypeError):
        master_id = None

    try:
        alter_id = int(str(safe_text(ledger, "ALTERID") or "0").strip())
    except (ValueError, AttributeError, TypeError):
        alter_id = 0

    # ledger_type = parent group from Tally
    ledger_type = safe_text(ledger, "PARENT") or None
    if ledger_type and ("charge" in ledger_type.lower() or "expense" in ledger_type.lower()):
        ledger_type = "Indirect Expenses"

    # --- Alias ---
    alias = None

    language_list = ledger.find("LANGUAGENAME.LIST")

    if language_list is not None:

        names = []

        for name_node in language_list.findall(".//NAME"):
            value = (name_node.text or "").strip()
            if value:
                names.append(value)

        if len(names) > 1:
            alias = names[1]

    if not alias:
        alias = safe_text(ledger, "BASICALIASNAME")

    if not alias:
        alias = safe_text(ledger, "ALTERNATENAME")

    # --- Mailing / address details ---
    address_lines = []
    city = state = country = pincode = None

    mailing = ledger.find("LEDMAILINGDETAILS.LIST")
    if mailing is None:
        mailing = ledger

    if mailing is not None:

        addr_list = mailing.find("ADDRESS.LIST")

        if addr_list is not None:
            address_lines = [
                (a.text or "").strip()
                for a in addr_list.findall("ADDRESS")
                if (a.text or "").strip()
            ]

        state_node = mailing.find("STATE")
        state = (
            (state_node.text or "").strip()
            if state_node is not None
            else None
        )

        country_node = mailing.find("COUNTRY")
        country = (
            (country_node.text or "").strip()
            if country_node is not None
            else None
        )

        pincode = safe_text(mailing, "PINCODE")

        city = (
            safe_text(mailing, "CITY")
            or safe_text(mailing, "PLACEOFSUPPLY")
            or None
        )

    address = "\n".join(address_lines) if address_lines else None

    mailing_name = safe_text(ledger, "MAILINGNAME") or customer_name

    # --- Contact details ---
    mobile_number = (
        safe_text(ledger, "MOBILE")
        or safe_text(ledger, "MOBILENO")
        or safe_text(ledger, "LEDGERMOBILE")
        or safe_text(ledger, "PARTYMOBILENO")
    )

    email = (
        safe_text(ledger, "EMAIL")
        or safe_text(ledger, "PARTYEMAIL")
    )

    contact_person = (
        safe_text(ledger, "CONTACTPERSON")
        or safe_text(ledger, "CONTACT")
    )

    # --- GST / PAN ---
    gst_number = None
    gst_details = ledger.find("LEDGSTREGDETAILS.LIST")

    if gst_details is not None:
        gst_number = safe_text(gst_details, "GSTIN")

    pan_number = (
        safe_text(ledger, "PANNUMBER")
        or safe_text(ledger, "PAN")
    )

    # --- Financial ---
    ob_raw = safe_text(ledger, "OPENINGBALANCE")
    opening_balance = parse_signed_balance(ob_raw)

    cb_raw = safe_text(ledger, "CLOSINGBALANCE")
    closing_balance = parse_signed_balance(cb_raw)

    credit_limit = parse_amount(safe_text(ledger, "CREDITLIMIT"))

    credit_period_raw = safe_text(ledger, "CREDITPERIOD")
    credit_period = credit_period_raw if credit_period_raw else None

    customer_discount_percentage = (
        parse_amount(
            safe_text(ledger, "DISCOUNTPERCENTAGE")
        ) or 0
    )

    return {

        "customer_name":
            customer_name,

        "ledger_type":
            ledger_type,

        "address":
            address,

        "city":
            city,

        "state":
            state,

        "pincode":
            pincode,

        "country":
            country,

        "contact_person":
            contact_person,

        "mobile_number":
            mobile_number,

        "email":
            email,

        "gst_number":
            gst_number,

        "pan_number":
            pan_number,

        "mailing_name":
            mailing_name,

        "alias":
            alias,

        "credit_period":
            credit_period,

        "credit_limit":
            credit_limit,

        "opening_balance":
            round(opening_balance, 2) if opening_balance else 0,

        "closing_balance":
            round(closing_balance, 2) if closing_balance else 0,

        "customer_discount_percentage":
            customer_discount_percentage,

        "company_name":
            company_name,

        "guid":
            guid,

        "master_id":
            master_id,
            
        "alter_id":
            alter_id,

        "is_active":
            True,

        "updated_at":
            datetime.now(
                timezone.utc
            ).isoformat(),
    }


def _fetch_customer_group(group_name, config, company_name, use_alter_id=True):
    max_alter_id = get_max_alter_id("customers", company_name) if use_alter_id else 0
    filter_tag = "<FILTER>AlterIdFilter</FILTER>" if (use_alter_id and max_alter_id > 0) else ""
    formula_tag = f'<SYSTEM TYPE="Formulae" NAME="AlterIdFilter">$AlterId &gt; {max_alter_id}</SYSTEM>' if (use_alter_id and max_alter_id > 0) else ""
    
    # Always compute closing as of today so Cash/Bank match Tally Cash/Bank Summary
    today_str = datetime.now().strftime("%Y%m%d")
    # BELONGSTO=Yes includes nested ledgers under the group (not only direct children)
    child_tags = ""
    if group_name:
        child_tags = f"<CHILDOF>{escape(group_name)}</CHILDOF>\n                        <BELONGSTO>Yes</BELONGSTO>"

    xml_request = f"""
<ENVELOPE>
    <HEADER>
        <VERSION>1</VERSION>
        <TALLYREQUEST>Export</TALLYREQUEST>
        <TYPE>Collection</TYPE>
        <ID>Ledger Collection</ID>
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
                    <COLLECTION NAME="Ledger Collection">
                        <TYPE>Ledger</TYPE>
                        {child_tags}
                        <FETCH>NAME, PARENT, OPENINGBALANCE, CLOSINGBALANCE, MAILINGNAME.LIST, ADDRESS.LIST, STATE, COUNTRY, PINCODE, CITY, PLACEOFSUPPLY, MOBILE, MOBILENO, LEDGERMOBILE, PARTYMOBILENO, PHONENUMBER, LEDGERPHONE, PARTYPHONENO, EMAIL, PARTYEMAIL, CONTACTPERSON, CONTACT, LEDGSTREGDETAILS.LIST, CREDITLIMIT, CREDITPERIOD, LEDGERCODE, ALTERID, MASTERID, GUID</FETCH>
                        {filter_tag}
                    </COLLECTION>
                    {formula_tag}
                </TDLMESSAGE>
            </TDL>
        </DESC>
    </BODY>
</ENVELOPE>
"""
    tally_url = config["tally_url"]
    response = requests.post(
        tally_url,
        data=xml_request.encode("utf-8"),
        headers={"Content-Type": "application/xml", "Connection": "close"},
        timeout=120
    )
    response.raise_for_status()

    response_text = clean_tally_xml(response.text)
    root = ET.fromstring(response_text)
    
    group_customers = []
    for ledger in root.findall(".//COLLECTION/LEDGER"):
        name = ledger.attrib.get("NAME", "").strip()
        if not name:
            continue
        data = extract_customer_data(ledger, company_name)
        group_customers.append(data)
        
    return group_customers

def sync_cash_bank_balances(company_name: str, config=None) -> int:
    """
    Force-refresh Cash / Bank / Bank OD closing balances as of today into local SQLite.
    Scoped by company_name. Marks changed rows pending for Phase 3 upload.
    """
    try:
        from .local_db import get_connection, _require_company_name
        company_name = _require_company_name(company_name, "cash_bank_balances")
        if not config:
            config = load_config()

        groups = [
            ("Bank Accounts", "Bank Accounts"),
            ("Bank OD A/c", "Bank OD A/c"),
            ("Bank OCC A/c", "Bank OD A/c"),
            ("Cash-in-Hand", "Cash-in-Hand"),
        ]
        updated = 0
        conn = get_connection()
        now_iso = datetime.now(timezone.utc).isoformat()
        try:
            with conn:
                for group_name, ledger_type in groups:
                    rows = _fetch_customer_group(group_name, config, company_name, use_alter_id=False)
                    for row in rows:
                        guid = row.get("guid")
                        if not guid:
                            continue
                        closing_balance = round(float(row.get("closing_balance") or 0), 2)
                        opening_balance = round(float(row.get("opening_balance") or 0), 2)
                        cur = conn.execute(
                            'UPDATE "customers" SET closing_balance = ?, opening_balance = ?, ledger_type = ?, '
                            '_sync_status = "pending", _synced_at = NULL '
                            'WHERE company_name = ? AND guid = ? AND ('
                            'closing_balance IS NULL OR closing_balance != ? OR '
                            'ledger_type IS NULL OR ledger_type != ?)',
                            (
                                closing_balance,
                                opening_balance,
                                ledger_type,
                                company_name,
                                guid,
                                closing_balance,
                                ledger_type,
                            ),
                        )
                        updated += cur.rowcount
                        # If row missing locally (first sync race), ignore — main save covers it
        finally:
            conn.close()

        if updated:
            logging.info(f"[LocalDB] Refreshed {updated} cash/bank balances for {company_name}")
        return updated
    except Exception as e:
        logging.warning(f"Cash/bank balance refresh failed: {e}")
        return 0


def sync_customer_balances(company_name: str, config=None, supabase=None):
    """
    Lightweight balance feed for all ledgers as of today.
    Updates local SQLite only for this company_name.
    """
    try:
        from .local_db import get_connection, _require_company_name
        company_name = _require_company_name(company_name, "customer_balances")
        if not config:
            config = load_config()
        tally_url = config["tally_url"]
        today_str = datetime.now().strftime("%Y%m%d")

        xml_request = f"""
<ENVELOPE>
    <HEADER>
        <VERSION>1</VERSION>
        <TALLYREQUEST>Export</TALLYREQUEST>
        <TYPE>Collection</TYPE>
        <ID>Ledger Balances Collection</ID>
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
                    <COLLECTION NAME="Ledger Balances Collection">
                        <TYPE>Ledger</TYPE>
                        <FETCH>NAME, GUID, CLOSINGBALANCE</FETCH>
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
            timeout=180
        )
        response.raise_for_status()
        response_text = clean_tally_xml(response.text)
        root = ET.fromstring(response_text)

        conn = get_connection()
        updated_count = 0
        try:
            with conn:
                for ledger in root.findall(".//COLLECTION/LEDGER"):
                    guid = safe_text(ledger, "GUID")
                    if not guid:
                        continue

                    cb_raw = safe_text(ledger, "CLOSINGBALANCE")
                    closing_balance = round(parse_signed_balance(cb_raw), 2)

                    cur = conn.execute(
                        'UPDATE "customers" SET closing_balance = ?, _sync_status = "pending", _synced_at = NULL '
                        'WHERE company_name = ? AND guid = ? AND (closing_balance IS NULL OR closing_balance != ?)',
                        (closing_balance, company_name, guid, closing_balance)
                    )
                    updated_count += cur.rowcount
        finally:
            conn.close()

        if updated_count > 0:
            logging.info(f"[LocalDB] Updated balances for {updated_count} ledger(s) locally ({company_name}).")
        return updated_count

    except Exception as e:
        logging.warning(f"Customer balance feed failed: {e}")
        return 0

def get_customers(company_name: str = None):
    config = load_config()
    if not company_name:
        company_details = get_company_details()
        company_name = company_details.get("company_name", "")

    # Incremental sync for general customers & suppliers (using AlterID)
    # Now also uses SVTODATE so closing balances are as of today
    all_customers = _fetch_customer_group(None, config, company_name, use_alter_id=True)
    cust_map = {c["guid"]: c for c in all_customers if c.get("guid")}
    
    # Always fetch live Bank and Cash ledgers WITHOUT AlterID (nested via BELONGSTO)
    bank_ledgers = _fetch_customer_group("Bank Accounts", config, company_name, use_alter_id=False)
    for b in bank_ledgers:
        if b.get("guid"):
            b["ledger_type"] = "Bank Accounts"
            cust_map[b["guid"]] = b

    bank_od_ledgers = _fetch_customer_group("Bank OD A/c", config, company_name, use_alter_id=False)
    for b in bank_od_ledgers:
        if b.get("guid"):
            b["ledger_type"] = "Bank OD A/c"
            cust_map[b["guid"]] = b

    bank_occ_ledgers = _fetch_customer_group("Bank OCC A/c", config, company_name, use_alter_id=False)
    for b in bank_occ_ledgers:
        if b.get("guid"):
            b["ledger_type"] = "Bank OD A/c"
            cust_map[b["guid"]] = b
            
    cash_ledgers = _fetch_customer_group("Cash-in-Hand", config, company_name, use_alter_id=False)
    for c in cash_ledgers:
        if c.get("guid"):
            c["ledger_type"] = "Cash-in-Hand"
            cust_map[c["guid"]] = c
    
    final_customers = list(cust_map.values())
    logging.info(
        f"  Total ledgers to sync: {len(final_customers)} (incremental customers + live bank/cash)"
    )
    logging.info(
        f"  Live cash/bank ledgers refreshed: "
        f"bank={len(bank_ledgers)} od={len(bank_od_ledgers)+len(bank_occ_ledgers)} cash={len(cash_ledgers)}"
    )

    return final_customers


def save_customers_to_supabase(customers, company_name: str = None):
    if not customers:
        return 0

    comp_name = company_name or (customers[0].get("company_name", "") if customers else "")

    from .local_db import save_to_local_db
    staged = save_to_local_db("customers", customers, comp_name)
    logging.info(f"[LocalDB] Staged {staged} customers locally in sync_cache.db")
    # Re-apply live cash/bank closing as of today (guards against stale incremental merges)
    try:
        sync_cash_bank_balances(comp_name)
    except Exception as e:
        logging.warning(f"Post-save cash/bank balance refresh skipped: {e}")
    return staged


def reconcile_deleted_customers(company_name: str) -> int:
    """
    Fetches all active Ledger GUIDs from Tally, compares them against
    Supabase 'customers', and deletes any ledgers that were deleted in Tally.
    """
    config = load_config()
    tally_url = config.get("tally_url", "http://localhost:9000")

    xml_request = f"""<ENVELOPE>
    <HEADER>
        <VERSION>1</VERSION>
        <TALLYREQUEST>Export</TALLYREQUEST>
        <TYPE>Collection</TYPE>
        <ID>AllLedgerGuids</ID>
    </HEADER>
    <BODY>
        <DESC>
            <STATICVARIABLES>
                <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
                <SVCURRENTCOMPANY>{escape(company_name)}</SVCURRENTCOMPANY>
            </STATICVARIABLES>
            <TDL>
                <TDLMESSAGE>
                    <COLLECTION NAME="AllLedgerGuids">
                        <TYPE>Ledger</TYPE>
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
        clean_xml = clean_tally_xml(response.text)
        root = ET.fromstring(clean_xml)

        tally_active_guids = set()
        for ledger in root.iter("LEDGER"):
            if safe_text(ledger, "ISDELETED") == "Yes":
                continue
            g = safe_text(ledger, "GUID") or ledger.attrib.get("GUID")
            if g:
                tally_active_guids.add(g.strip())

    except Exception as e:
        logging.warning(f"Could not fetch active Ledger GUIDs from Tally for reconciliation: {e}")
        return 0

    from .local_db import reconcile_local_deletions
    return reconcile_local_deletions("customers", tally_active_guids, company_name)




# =====================================================
# MAIN
# =====================================================

if __name__ == "__main__":

    import sys

    try:

        sync_type = sys.argv[1] if len(sys.argv) > 1 else "customers"

        if sync_type == "customers":

            logging.info("Fetching customers from Tally...")
            customers = get_customers()
            logging.info(f"Found {len(customers)} total records.")

            logging.info("Saving to Supabase...")
            save_customers_to_supabase(customers)
            logging.info(f"\nCustomers synced successfully ({len(customers)} records).")

        else:

            logging.info("Fetching ledgers from Tally...")
            ledgers = get_ledgers()
            logging.info(f"Found {len(ledgers)} ledgers.")

            logging.info("Saving to Supabase...")
            save_ledgers_to_supabase(ledgers)
            logging.info(f"\nLedgers synced successfully ({len(ledgers)} records).")

    except Exception as e:

        logging.info(f"Sync failed: {e}")

        raise
