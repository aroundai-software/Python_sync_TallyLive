from html.parser import HTMLParser
import re, pathlib
# re-fetch HTML quickly
import requests
from xml.sax.saxutils import escape
TALLY="http://127.0.0.1:9000"
COMPANY="AL ZAHRA DEMO EQIPMENT"
xml=f"""<ENVELOPE><HEADER><VERSION>1</VERSION><TALLYREQUEST>Export</TALLYREQUEST>
<TYPE>Data</TYPE><ID>Cash Flow</ID></HEADER>
<BODY><DESC><STATICVARIABLES>
<SVEXPORTFORMAT>$$SysName:HTML</SVEXPORTFORMAT>
<SVCURRENTCOMPANY>{escape(COMPANY)}</SVCURRENTCOMPANY>
<SVFROMDATE TYPE="Date">01-Apr-2026</SVFROMDATE>
<SVTODATE TYPE="Date">30-Apr-2026</SVTODATE>
</STATICVARIABLES></DESC></BODY></ENVELOPE>"""
r=requests.post(TALLY,data=xml.encode(),headers={"Content-Type":"application/xml","Connection":"close"},timeout=60)
t=r.content.decode("utf-16","replace") if "utf-16" in r.content[:80].decode("latin1","replace").lower() else r.content.decode("utf-8","replace")
# try both encodings
for enc in ["utf-16","utf-8","utf-16-le"]:
    try:
        t2=r.content.decode(enc)
        if "Current" in t2 or "Inflow" in t2 or "April" in t2 or "Cash" in t2:
            t=t2
            print("encoding", enc, "len", len(t))
            break
    except Exception as e:
        print("enc fail", enc, e)

# strip tags to text
text=re.sub(r"<[^>]+>", "\n", t)
text=re.sub(r"&nbsp;|&#160;", " ", text)
text=re.sub(r"\n+", "\n", text)
lines=[ln.strip() for ln in text.splitlines() if ln.strip()]
print("LINES", len(lines))
for ln in lines[:120]:
    print(repr(ln)[:120])
# search key amounts
for key in ["698515","696622","211043","Current Assets","Current Liabilities","Indirect","Inflow","Outflow","Nett"]:
    if key.lower() in t.lower() or key in "".join(lines):
        print("HIT", key)
pathlib.Path("_cf_april_text.txt").write_text("\n".join(lines), encoding="utf-8")
