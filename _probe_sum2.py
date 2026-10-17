import requests, re
from xml.sax.saxutils import escape
TALLY = "http://127.0.0.1:9000"
COMPANY = "AL ZAHRA DEMO EQIPMENT"

def post(xml, timeout=45):
    r = requests.post(TALLY, data=xml.encode(), headers={"Content-Type":"application/xml","Connection":"close"}, timeout=timeout)
    return r.content.decode("utf-8", "replace")

# 1) HTML export of Cash Flow for April
xml_html = f"""<ENVELOPE><HEADER><VERSION>1</VERSION><TALLYREQUEST>Export</TALLYREQUEST>
<TYPE>Data</TYPE><ID>Cash Flow</ID></HEADER>
<BODY><DESC><STATICVARIABLES>
<SVEXPORTFORMAT>$$SysName:HTML</SVEXPORTFORMAT>
<SVCURRENTCOMPANY>{escape(COMPANY)}</SVCURRENTCOMPANY>
<SVFROMDATE TYPE="Date">01-Apr-2026</SVFROMDATE>
<SVTODATE TYPE="Date">30-Apr-2026</SVTODATE>
</STATICVARIABLES></DESC></BODY></ENVELOPE>"""
t = post(xml_html)
print("HTML bytes", len(t))
print(t[:2500])
open("_cf_april.html","w",encoding="utf-8").write(t)

# 2) Excel / SDF
for fmt in ["$$SysName:Excel", "$$SysName:SDF", "$$SysName:ASCII", "$$SysName:XML"]:
    xml = f"""<ENVELOPE><HEADER><VERSION>1</VERSION><TALLYREQUEST>Export</TALLYREQUEST>
<TYPE>Data</TYPE><ID>Cash Flow</ID></HEADER>
<BODY><DESC><STATICVARIABLES>
<SVEXPORTFORMAT>{fmt}</SVEXPORTFORMAT>
<SVCURRENTCOMPANY>{escape(COMPANY)}</SVCURRENTCOMPANY>
<SVFROMDATE TYPE="Date">01-Apr-2026</SVFROMDATE>
<SVTODATE TYPE="Date">30-Apr-2026</SVTODATE>
</STATICVARIABLES></DESC></BODY></ENVELOPE>"""
    t = post(xml)
    err = re.search(r"<LINEERROR>(.*?)</LINEERROR>", t, re.I|re.S)
    print(f"\nFMT {fmt} bytes={len(t)} err={bool(err)} sample={t[:200]!r}")

# 3) Try TYPE Collection List of Cash Flow related
coll_xml = f"""<ENVELOPE><HEADER><VERSION>1</VERSION><TALLYREQUEST>Export</TALLYREQUEST>
<TYPE>Collection</TYPE><ID>Cash Flow</ID></HEADER>
<BODY><DESC><STATICVARIABLES>
<SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
<SVCURRENTCOMPANY>{escape(COMPANY)}</SVCURRENTCOMPANY>
<SVFROMDATE TYPE="Date">01-Apr-2026</SVFROMDATE>
<SVTODATE TYPE="Date">30-Apr-2026</SVTODATE>
</STATICVARIABLES></DESC></BODY></ENVELOPE>"""
t = post(coll_xml)
print("\nCollection Cash Flow bytes", len(t), t[:800])
