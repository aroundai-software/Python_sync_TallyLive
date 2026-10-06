import logging
import re
import requests
import xmltodict
from datetime import datetime, timezone

from supabase import create_client

from .config import load_config
from .source_values import clean_source_xml, company_currency, decode_response


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
    """Return text or None if empty"""
    text = get_text(value)
    return text if text else None


def format_tally_date(value):

    value = get_text(value)

    if not value:
        return None

    try:

        return datetime.strptime(
            value,
            "%Y%m%d"
        ).date().isoformat()

    except Exception:

        return None


def get_address_parts(item):

    # Try COMPUTE fields first (ADDR1, ADDR2, ADDR3)
    computed = [
        clean_text(item.get("ADDR1", "")),
        clean_text(item.get("ADDR2", "")),
        clean_text(item.get("ADDR3", "")),
    ]

    computed = [
        a for a in computed if a
    ]

    if computed:
        return computed

    # Fallback: try BASICCOMPANYADDRESS.LIST
    address_list = item.get(
        "BASICCOMPANYADDRESS.LIST"
    ) or {}

    if isinstance(address_list, list):

        address_list = (
            address_list[0]
            if address_list
            else {}
        )

    addresses = address_list.get(
        "BASICCOMPANYADDRESS", []
    )

    if isinstance(addresses, str):
        addresses = [addresses]

    return [
        clean_text(a)
        for a in addresses
        if clean_text(a)
    ]


def clean_tally_xml(xml_text):
    return clean_source_xml(xml_text)


# =====================================================
# TALLY FETCH
# =====================================================

def extract_company_dict(company):
    address_parts = get_address_parts(company)
    books_from = get_text(company.get("BOOKSFROM"))

    return {
        "company_name": get_text(company.get("NAME")),
        "company_number": get_text_or_none(company.get("COMPANYNUMBER")),
        "guid": get_text(company.get("GUID")),
        "master_id": get_text_or_none(company.get("MASTERID")),
        "address_line1": address_parts[0] if len(address_parts) > 0 else None,
        "address_line2": address_parts[1] if len(address_parts) > 1 else None,
        "city": address_parts[2] if len(address_parts) > 2 else None,
        "emirate": get_text_or_none(company.get("STATENAME")),
        "country": get_text_or_none(company.get("COUNTRYNAME")),
        "pin_code": get_text_or_none(company.get("PINCODE")),
        "mobile_number": (
            get_text_or_none(company.get("MOBILENO"))
            or get_text_or_none(company.get("COMPANYCONTACTNUMBER"))
            or get_text_or_none(company.get("PHONENUMBER"))
        ),
        "phone_number": get_text_or_none(company.get("PHONENUMBER")),
        "email": get_text_or_none(company.get("EMAIL")),
        "website": get_text_or_none(company.get("WEBSITE")),
        "trn_number": get_text_or_none(company.get("GSTREGISTRATIONNUMBER")),
        "trade_license_number": get_text_or_none(company.get("CRNUMBER")),
        "trade_license_expiry": None,
        "base_currency": company_currency(company),
        "books_from": format_tally_date(books_from),
        "financial_year_from": format_tally_date(books_from),
        "is_active": True,
        "sync_enabled": True,
        "updated_at": datetime.now(timezone.utc).isoformat()
    }

def get_all_companies_details():
    xml_request = """
<ENVELOPE>
    <HEADER>
        <VERSION>1</VERSION>
        <TALLYREQUEST>Export</TALLYREQUEST>
        <TYPE>Collection</TYPE>
        <ID>Company Collection</ID>
    </HEADER>
    <BODY>
        <DESC>
            <TDL>
                <TDLMESSAGE>
                    <COLLECTION NAME="Company Collection">
                        <TYPE>Company</TYPE>
                        <FETCH>
                            *,
                            MOBILENO,
                            MASTERID
                        </FETCH>
                    </COLLECTION>
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
        timeout=60
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

    companies = collection.get("COMPANY", [])
    if isinstance(companies, dict):
        companies = [companies]

    if not companies:
        raise Exception("No company found in Tally")

    result = []
    for c in companies:
        c_dict = extract_company_dict(c)
        if c_dict.get("company_name"):
            result.append(c_dict)

    return result

def get_company_details():
    all_companies = get_all_companies_details()
    if not all_companies:
        raise Exception("No company found in Tally")
    return all_companies[0]


# =====================================================
# SUPABASE SAVE
# =====================================================

def save_company_to_supabase(company_data):
    from .local_db import save_to_local_db
    comp_name = company_data.get("company_name", "") if isinstance(company_data, dict) else ""
    if company_data:
        save_to_local_db("tally_companies", [company_data], comp_name)
    return True


# =====================================================
# MAIN
# =====================================================

if __name__ == "__main__":

    try:

        logging.info("Fetching company from Tally...")

        company_data = get_company_details()

        logging.info(f"Found company: {company_data.get('company_name')}")

        logging.info("Saving to Supabase...")

        save_company_to_supabase(company_data)

        logging.info("Company synced successfully.")

    except Exception as e:

        logging.info(f"Company sync failed: {e}")

        raise