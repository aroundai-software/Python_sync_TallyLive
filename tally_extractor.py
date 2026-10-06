import requests
import xmltodict
import json
import os
import logging
from src.config import load_config

config_data = load_config()
TALLY_PORT = config_data.get("tally_port", 9000)
TALLY_URL = f"http://localhost:{TALLY_PORT}"

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def get_companies():
    """Fetch companies from Tally."""
    fields = [
        "NAME", "GUID", "COMPANYNUMBER", "STATENAME", 
        "PINCODE", "EMAIL", "PHONENUMBER", 
        "INCOMETAXNUMBER", "ADDRESS"
    ]
    return fetch_collection("Company", fields)

def fetch_collection(collection_name, fields):
    """
    Fetch a standard collection (like Ledgers or StockItems) from Tally 
    dynamically using TDL in XML.
    """
    # Generating TDL dynamically to fetch specific fields
    tdl_fields = "".join([f'<FIELD NAME="{f}"><SET>${f}</SET><XMLTAG>"{f}"</XMLTAG></FIELD>\n' for f in fields])
    field_names = ",".join(fields)
    
    xml_request = f'''<ENVELOPE>
      <HEADER>
        <TALLYREQUEST>Export Data</TALLYREQUEST>
      </HEADER>
      <BODY>
        <EXPORTDATA>
          <REQUESTDESC>
            <STATICVARIABLES>
              <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
            </STATICVARIABLES>
            <TDL>
              <TDLMESSAGE>
                <REPORT NAME="CustomReport">
                  <FORMS>CustomForm</FORMS>
                </REPORT>
                <FORM NAME="CustomForm">
                  <PARTS>CustomPart</PARTS>
                </FORM>
                <PART NAME="CustomPart">
                  <LINES>CustomLine</LINES>
                  <REPEAT>CustomLine : {collection_name}</REPEAT>
                  <SCROLLED>Vertical</SCROLLED>
                </PART>
                <LINE NAME="CustomLine">
                  <FIELDS>{field_names}</FIELDS>
                </LINE>
                {tdl_fields}
              </TDLMESSAGE>
            </TDL>
          </REQUESTDESC>
        </EXPORTDATA>
      </BODY>
    </ENVELOPE>'''

    try:
        logging.info(f"Fetching {collection_name} from Tally...")
        response = requests.post(TALLY_URL, data=xml_request.encode('utf-8'), headers={'Content-Type': 'text/xml;charset=utf-8'})
        if response.status_code == 200:
            # Parse XML
            data = xmltodict.parse(response.text)
            
            # Navigate structure based on standard Tally XML wrapper
            # Tally returns <ENVELOPE><BODY><DATA><COLLECTION><CustomLine>...
            try:
                collection_data = data.get('ENVELOPE', {}).get('BODY', {}).get('DATA', {}).get('COLLECTION', {}).get('CustomLine', [])
                if not isinstance(collection_data, list):
                    collection_data = [collection_data]
                logging.info(f"Fetched {len(collection_data)} items for {collection_name}.")
                return collection_data
            except AttributeError:
                logging.warning(f"No data found for {collection_name}")
                return []
        else:
            logging.error(f"Failed to fetch {collection_name}: Status {response.status_code}")
    except Exception as e:
        logging.error(f"Connection error fetching {collection_name}: {e}")
    return []

def get_customers():
    # To fetch customers, we fetch Ledgers filtered by Sundry Debtors (group logic simplified)
    fields = ["NAME", "GUID", "PARENT", "LEDGERPHONE", "ADDRESS", "PINCODE", "STATENAME", "PARTYGSTIN"]
    return fetch_collection("Ledger", fields)

def get_products():
    fields = ["NAME", "GUID", "PARENT", "BASEUNITS", "RATEOFVAT", "OPENINGBALANCE", "DESCRIPTION", "PARTNO"]
    return fetch_collection("StockItem", fields)

def get_categories():
    fields = ["NAME", "GUID", "PARENT"]
    return fetch_collection("Group", fields)

def get_item_parents():
    fields = ["NAME", "GUID", "PARENT"]
    return fetch_collection("StockGroup", fields)
