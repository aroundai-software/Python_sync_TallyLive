import requests, re, xml.etree.ElementTree as ET
from xml.sax.saxutils import escape
from collections import defaultdict
TALLY="http://127.0.0.1:9000"
COMPANY="AL ZAHRA DEMO EQIPMENT"

def post(xml, timeout=120):
    r=requests.post(TALLY,data=xml.encode(),headers={"Content-Type":"application/xml","Connection":"close"},timeout=timeout)
    return r.content.decode("utf-8","replace")

# Collection of Vouchers for April with ledger entries - use TDL collection definition in request
xml = f"""<ENVELOPE>
<HEADER><VERSION>1</VERSION><TALLYREQUEST>Export</TALLYREQUEST><TYPE>Data</TYPE><ID>MyCashVouchers</ID></HEADER>
<BODY>
<DESC>
<STATICVARIABLES>
<SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
<SVCURRENTCOMPANY>{escape(COMPANY)}</SVCURRENTCOMPANY>
<SVFROMDATE TYPE="Date">20260401</SVFROMDATE>
<SVTODATE TYPE="Date">20260430</SVTODATE>
</STATICVARIABLES>
<TDL>
<TDLMESSAGE>
<REPORT NAME="MyCashVouchers">
  <FORMS>MyCashVouchers</FORMS>
</REPORT>
<FORM NAME="MyCashVouchers">
  <PARTS>MyCashVouchers</PARTS>
</FORM>
<PART NAME="MyCashVouchers">
  <LINES>MyCashVouchers</LINES>
  <REPEAT>MyCashVouchers : MyCashVouchersColl</REPEAT>
  <SCROLLED>Vertical</SCROLLED>
</PART>
<LINE NAME="MyCashVouchers">
  <FIELDS>FldDate,FldVchType,FldVchNo</FIELDS>
  <EXPLODE>MyCashLedgers : Yes</EXPLODE>
</LINE>
<LINE NAME="MyCashLedgers">
  <FIELDS>FldLedName,FldAmount</FIELDS>
  <REPEAT>MyCashLedgers : AllLedgerEntries</REPEAT>
</LINE>
<FIELD NAME="FldDate"><SET>$Date</SET></FIELD>
<FIELD NAME="FldVchType"><SET>$VoucherTypeName</SET></FIELD>
<FIELD NAME="FldVchNo"><SET>$VoucherNumber</SET></FIELD>
<FIELD NAME="FldLedName"><SET>$LedgerName</SET></FIELD>
<FIELD NAME="FldAmount"><SET>$Amount</SET></FIELD>
<COLLECTION NAME="MyCashVouchersColl">
  <TYPE>Voucher</TYPE>
  <FILTER>IsInPeriod</FILTER>
</COLLECTION>
<SYSTEM TYPE="Formulae" NAME="IsInPeriod">$$IsBetween:$Date:@@SVFromDate:@@SVToDate</SYSTEM>
</TDLMESSAGE>
</TDL>
</DESC>
</BODY>
</ENVELOPE>"""
print("posting TDL...")
t=post(xml)
print("bytes", len(t))
err=re.search(r"<LINEERROR>(.*?)</LINEERROR>", t, re.I|re.S)
print("err", (err.group(1)[:300] if err else None))
print(t[:2500])
# count field values
print("FldLedName count", len(re.findall(r"<FldLedName>", t, re.I)))
print("Current Assets mentions", len(re.findall(r"Current Assets", t, re.I)))
