import logging
import re
import uuid
import requests
import xmltodict
from datetime import datetime, timezone, timedelta
from xml.sax.saxutils import escape

from supabase import create_client

from .config import load_config
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


def get_text_or_none(value):
    """Return text or None if empty."""
    text = get_text(value)
    return text if text else None


def safe_float(value, default=0.0):

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


def julian_to_date(jd_value):
    """
    Tally stores due dates as Julian Day Numbers
    in the @JD attribute of BILLCREDITPERIOD.
    Tally uses the same epoch as Excel/Delphi:
    Dec 31, 1899.
    """

    try:

        jd = int(str(jd_value).strip())

        if jd <= 0:
            return None

        epoch = datetime(1899, 12, 31)

        result = epoch + timedelta(days=jd)

        return result.date().isoformat()

    except Exception:

        return None


def resolve_due_date_and_credit_days(bill, invoice_date):
    """
    Accurately resolves Due Date, Credit Days, and Overdue Days from Tally.
    Handles:
    1. Direct BILLDUEDATE or DUEDATE if exported by Tally.
    2. @JD attribute in BILLCREDITPERIOD (Julian Day).
    3. Plain text strings in BILLCREDITPERIOD (e.g. '19 Days', '30 days', '1 Month').
    4. Explicit date strings in BILLCREDITPERIOD (e.g. '12-Apr-20').
    5. Fallback to invoice_date when no credit period was configured in Tally.
    """
    if not invoice_date:
        return None, 0, 0
    inv_date_obj = datetime.fromisoformat(invoice_date).date()
    today = datetime.now(timezone.utc).date()

    # 1. Direct Due Date from Tally
    raw_due = get_text(bill.get("BILLDUEDATE")) or get_text(bill.get("DUEDATE"))
    if raw_due:
        parsed_due = format_tally_date(raw_due)
        if parsed_due:
            try:
                due_dt = datetime.fromisoformat(parsed_due).date()
                credit_days = max(0, (due_dt - inv_date_obj).days)
                overdue_days = max(0, (today - due_dt).days) if today > due_dt else 0
                return parsed_due, credit_days, overdue_days
            except Exception:
                pass

    # 2. Check Julian Day Number in @JD
    credit_period = bill.get("BILLCREDITPERIOD")
    jd = ""
    if isinstance(credit_period, dict):
        jd = credit_period.get("@JD", "")
    elif isinstance(credit_period, list) and len(credit_period) > 0 and isinstance(credit_period[0], dict):
        jd = credit_period[0].get("@JD", "")

    if jd:
        julian_date = julian_to_date(jd)
        if julian_date:
            try:
                due_dt = datetime.fromisoformat(julian_date).date()
                credit_days = max(0, (due_dt - inv_date_obj).days)
                overdue_days = max(0, (today - due_dt).days) if today > due_dt else 0
                return julian_date, credit_days, overdue_days
            except Exception:
                pass

    # 3. String credit period (e.g. '19 Days', '30 days', '1 Month')
    credit_str = get_text(credit_period)
    if credit_str:
        month_match = re.search(r'(\d+)\s*(?:month|m)\b', credit_str, re.IGNORECASE)
        if month_match:
            days = int(month_match.group(1)) * 30
            due_dt = inv_date_obj + timedelta(days=days)
            overdue_days = max(0, (today - due_dt).days) if today > due_dt else 0
            return due_dt.isoformat(), days, overdue_days

        day_match = re.search(r'(\d+)', credit_str)
        if day_match:
            days = int(day_match.group(1))
            due_dt = inv_date_obj + timedelta(days=days)
            overdue_days = max(0, (today - due_dt).days) if today > due_dt else 0
            return due_dt.isoformat(), days, overdue_days

        for fmt in ('%d-%b-%y', '%d-%b-%Y', '%d/%m/%Y', '%d/%m/%y', '%Y-%m-%d'):
            try:
                due_dt = datetime.strptime(credit_str.strip(), fmt).date()
                credit_days = max(0, (due_dt - inv_date_obj).days)
                overdue_days = max(0, (today - due_dt).days) if today > due_dt else 0
                return due_dt.isoformat(), credit_days, overdue_days
            except ValueError:
                pass

    # 4. Fallback: No credit terms configured (0 days credit)
    overdue_days = max(0, (today - inv_date_obj).days) if today > inv_date_obj else 0
    return inv_date_obj.isoformat(), 0, overdue_days


def clean_tally_xml(xml_text):
    """Remove invalid XML character refs and fix stray ampersands."""

    if not xml_text:
        return ""

    xml_text = xml_text.replace('encoding="utf-16"', 'encoding="utf-8"')
    xml_text = xml_text.replace('\x00', '')

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


# =====================================================
# CUSTOMER LOOKUP
# =====================================================

def fetch_all_customers(supabase, company_name: str = None):
    """
    Read customers from local SQLite cache first (populated in Step 2).
    Falls back to Supabase only if local cache is empty.
    This avoids a Supabase dependency during the Tally phase when nothing
    has been uploaded to the cloud yet.
    """
    from .local_db import get_local_customers_map
    local_map = get_local_customers_map(company_name or "")
    if local_map:
        # Convert {name_lower: {id, ledger_type}} → list of dicts matching old Supabase shape
        return [
            {
                "customer_name": name,
                "master_id": info.get("id"),
                "ledger_type": info.get("ledger_type"),
                "mobile_number": None,
            }
            for name, info in local_map.items()
        ]

    # Fallback: query Supabase (used on second+ syncs when data is already uploaded)
    results = []
    page_size = 1000
    offset = 0

    while True:
        query = supabase.table("customers").select("master_id, customer_name, ledger_type, mobile_number")
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



# =====================================================
# TALLY FETCH
# =====================================================

def get_outstanding_receivables(company_name: str = None):
    if not company_name:
        company_details = get_company_details()
        company_name = company_details.get("company_name", "")

    today_str = datetime.now().strftime("%Y%m%d")

    xml_request = f"""
<ENVELOPE>

    <HEADER>
        <VERSION>1</VERSION>
        <TALLYREQUEST>Export</TALLYREQUEST>
        <TYPE>Collection</TYPE>
        <ID>Outstanding Collection</ID>
    </HEADER>

    <BODY>

        <DESC>
            <STATICVARIABLES>
                <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
                <SVCURRENTCOMPANY>{escape(company_name)}</SVCURRENTCOMPANY>
                <SVFROMDATE TYPE="Date">19000101</SVFROMDATE>
                <SVTODATE TYPE="Date">{today_str}</SVTODATE>
            </STATICVARIABLES>

            <TDL>

                <TDLMESSAGE>

                    <COLLECTION NAME="Outstanding Collection">

                        <TYPE>Bill</TYPE>

                        <FETCH>
                            NAME,
                            BILLDATE,
                            BILLCREDITPERIOD,
                            BILLDUEDATE,
                            OPENINGBALANCE,
                            CLOSINGBALANCE,
                            LEDGERNAME,
                            BILLID,
                            PARENT
                        </FETCH>

                        <FILTER>IsOutstandingReceivable</FILTER>

                    </COLLECTION>

                    <SYSTEM TYPE="Formulae" NAME="IsOutstandingReceivable">
                        $ClosingBalance != 0 AND $$IsDr:$ClosingBalance
                    </SYSTEM>

                </TDLMESSAGE>

            </TDL>

        </DESC>

    </BODY>

</ENVELOPE>
"""

    config = load_config()

    tally_url = config["tally_url"]

    supabase = create_client(
        config["supabase_url"],
        config["supabase_key"]
    )

    # -------------------------------------------------
    # CONNECT TO TALLY
    # -------------------------------------------------

    response = requests.post(
        tally_url,
        data=xml_request.encode("utf-8"),
        headers={
            "Content-Type": "application/xml", "Connection": "close"
        },
        timeout=180
    )

    response.raise_for_status()

    # -------------------------------------------------
    # CLEAN & PARSE XML
    # -------------------------------------------------

    xml_content = clean_tally_xml(response.text)

    data = xmltodict.parse(xml_content)

    # -------------------------------------------------
    # EXTRACT BILLS
    # -------------------------------------------------

    envelope = data.get("ENVELOPE") or {}
    body = envelope.get("BODY") or {}
    data_node = body.get("DATA") or {}
    collection = (data_node.get("COLLECTION") if isinstance(data_node, dict) else {}) or {}
    bills = collection.get("BILL", []) if isinstance(collection, dict) else []

    if isinstance(bills, dict):
        bills = [bills]

    if not bills:
        print("No Outstanding Bills Found")
        return []

    print(f"Found {len(bills)} outstanding bills")

    # -------------------------------------------------
    # BUILD CUSTOMER NAME → MASTER ID / GROUP / MOBILE MAP
    # -------------------------------------------------

    print("Fetching customer master IDs and details...")

    customer_to_master_id = {}
    customer_to_group = {}
    customer_to_mobile = {}

    try:

        for row in fetch_all_customers(supabase, company_name):
            c_name = row.get("customer_name")
            if c_name:
                key = c_name.strip().lower()
                customer_to_master_id[key] = row.get("master_id")
                customer_to_group[key] = row.get("ledger_type")
                customer_to_mobile[key] = row.get("mobile_number")

        print(f"Loaded {len(customer_to_master_id)} customer records")

    except Exception as e:

        print(f"Customer lookup error: {e}")

    # -------------------------------------------------
    # PROCESS BILLS
    # -------------------------------------------------

    now = datetime.now(timezone.utc).isoformat()
    outstandings_list = []

    for bill in bills:

        try:

            invoice_number = get_text(
                bill.get("NAME")
            )

            if not invoice_number:
                continue

            customer_name = get_text(
                bill.get("LEDGERNAME")
            )

            if not customer_name:
                customer_name = get_text(
                    bill.get("PARENT")
                )

            if not customer_name:
                continue

            # GUID: company-scoped to avoid collisions across multiple companies in Supabase
            company_prefix = company_name or "DEFAULT"
            bill_id = get_text(bill.get("BILLID"))
            if bill_id:
                guid = f"{company_prefix}#BILL-{bill_id}"
            else:
                guid = f"{company_prefix}#{customer_name}#{invoice_number}"

            invoice_date = format_tally_date(
                bill.get("BILLDATE")
            )

            if not invoice_date:
                continue

            # pending = abs(CLOSINGBALANCE)
            pending_amount = abs(
                safe_float(bill.get("CLOSINGBALANCE"))
            )

            if pending_amount == 0:
                continue

            # invoice = abs(OPENINGBALANCE)
            invoice_amount = abs(
                safe_float(bill.get("OPENINGBALANCE"))
            )

            if not invoice_amount:
                invoice_amount = pending_amount

            paid_amount = round(
                invoice_amount - pending_amount, 2
            )

            if paid_amount < 0:
                paid_amount = 0.0

            # Resolve ledger_master_id, group_name, and mobile from customers table
            customer_key = customer_name.strip().lower()
            ledger_master_id = customer_to_master_id.get(customer_key)

            group_name = customer_to_group.get(customer_key)
            if not group_name:
                parent_tag = get_text(bill.get("PARENT"))
                if parent_tag and parent_tag.strip().lower() != customer_key:
                    group_name = parent_tag.strip()
                else:
                    group_name = "Sundry Debtors"

            mobile_number = customer_to_mobile.get(customer_key)

            # Calculate due date, credit days, and overdue days accurately
            due_date, credit_days, overdue_days = resolve_due_date_and_credit_days(
                bill, invoice_date
            )

            row = {

                "customer_name":
                    customer_name,

                "group_name":
                    group_name,

                "mobile":
                    mobile_number,

                "date":
                    invoice_date,

                "invoicenumber":
                    invoice_number,

                "opening_balance":
                    round(invoice_amount, 2),

                "closing_balance":
                    round(pending_amount, 2),

                "amount":
                    round(pending_amount, 2),

                "duedate":
                    due_date,

                "overdue_days":
                    overdue_days,

                "credit_days":
                    credit_days,

                "bill_type":
                    "Sales",

                "company_name":
                    company_name,

                "guid":
                    guid,

                "master_id":
                    str(ledger_master_id)
                    if ledger_master_id
                    else None,

                "updated_at":
                    now,
            }

            outstandings_list.append(row)

        except Exception as e:

            print(f"Bill Error: {e}")

    # Deduplicate GUIDs to prevent any batch upsert collision within the company
    seen_guids = set()
    for r in outstandings_list:
        base_guid = r["guid"]
        g = base_guid
        idx = 1
        while g in seen_guids:
            g = f"{base_guid}_{idx}"
            idx += 1
        seen_guids.add(g)
        r["guid"] = g

    return outstandings_list


# =====================================================
# SUPABASE SAVE
# =====================================================

def save_outstanding_receivables_to_supabase(outstandings, company_name=None):
    from .local_db import save_to_local_db
    if not company_name and outstandings:
        company_name = outstandings[0].get("company_name", "")
    if outstandings:
        staged = save_to_local_db("outstanding_receivables", outstandings, company_name or "")
        logging.info(f"[LocalDB] Staged {staged} outstanding receivables locally in sync_cache.db")
        return staged
    return 0


# =====================================================
# MAIN
# =====================================================

if __name__ == "__main__":

    try:

        print("Fetching outstandings from Tally...")

        outstandings = get_outstanding_receivables()

        print(f"Found {len(outstandings)} outstandings.")

        print("Saving to Supabase...")

        save_outstanding_receivables_to_supabase(outstandings)

        print(
            f"\nOutstandings synced successfully ({len(outstandings)} records)."
        )

    except Exception as e:

        print(f"Outstanding sync failed: {e}")

        raise
