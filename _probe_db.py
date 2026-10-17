import requests, re, json
from xml.sax.saxutils import escape
from collections import defaultdict
TALLY = "http://127.0.0.1:9000"
COMPANY = "AL ZAHRA DEMO EQIPMENT"

def post(xml, timeout=90):
    r = requests.post(TALLY, data=xml.encode(), headers={"Content-Type":"application/xml","Connection":"close"}, timeout=timeout)
    return r.content.decode("utf-8", "replace")

# Fetch daybook/vouchers for April with ledger entries - try Day Book export
xml = f"""<ENVELOPE><HEADER><VERSION>1</VERSION><TALLYREQUEST>Export</TALLYREQUEST>
<TYPE>Data</TYPE><ID>Day Book</ID></HEADER>
<BODY><DESC><STATICVARIABLES>
<SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
<SVCURRENTCOMPANY>{escape(COMPANY)}</SVCURRENTCOMPANY>
<SVFROMDATE TYPE="Date">01-Apr-2026</SVFROMDATE>
<SVTODATE TYPE="Date">30-Apr-2026</SVTODATE>
</STATICVARIABLES></DESC></BODY></ENVELOPE>"""
print("fetching day book...")
t = post(xml)
print("bytes", len(t))
err = re.search(r"<LINEERROR>(.*?)</LINEERROR>", t, re.I|re.S)
print("err", err.group(1)[:200] if err else None)
# sample tags
tags = sorted(set(re.findall(r"</?([A-Z][A-Z0-9.]*)", t[:50000])))
print("sample tags", tags[:80])
print(t[:2000])
open("_daybook_apr.xml","w",encoding="utf-8").write(t[:200000])
