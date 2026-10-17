import requests, re
from xml.sax.saxutils import escape
from collections import defaultdict
import sqlite3
from src.source_values import clean_source_xml, decode_response
import xmltodict

TALLY="http://127.0.0.1:9000"
COMPANY="AL ZAHRA DEMO EQIPMENT"

# Map Tally reserved / common ledger_type -> Cash Flow Summary category (as Tally shows)
TO_PRIMARY = {
    "cash-in-hand": "Current Assets",
    "bank accounts": "Current Assets",
    "bank od a/c": "Current Assets",
    "bank od accounts": "Current Assets",
    "stock-in-hand": "Current Assets",
    "sundry debtors": "Current Assets",
    "deposits (asset)": "Current Assets",
    "loans & advances (asset)": "Current Assets",
    "current assets": "Current Assets",
    "sundry creditors": "Current Liabilities",
    "duties & taxes": "Current Liabilities",
    "provisions": "Current Liabilities",
    "current liabilities": "Current Liabilities",
    "duties and taxes": "Current Liabilities",
    "indirect expenses": "Indirect Expenses",
    "indirect incomes": "Indirect Incomes",
    "direct expenses": "Direct Expenses",
    "direct incomes": "Direct Incomes",
    "sales accounts": "Sales Accounts",
    "purchase accounts": "Purchase Accounts",
    "fixed assets": "Fixed Assets",
    "capital account": "Capital Account",
    "loans (liability)": "Loans (Liability)",
    "secured loans": "Loans (Liability)",
    "unsecured loans": "Loans (Liability)",
    "investments": "Investments",
    "suspense a/c": "Suspense A/c",
    "branch / divisions": "Branch / Divisions",
    "misc. expenses (asset)": "Misc. Expenses (Asset)",
}

def get_text(v):
    if v is None: return ""
    if isinstance(v, dict): return str(v.get("#text") or "").strip()
    return str(v).strip()

def safe_float(v):
    try:
        s=get_text(v).replace(",","")
        return float(s) if s else 0.0
    except: return 0.0

conn=sqlite3.connect("data/sync_cache.db")
conn.row_factory=sqlite3.Row
ledger_meta={}
for r in conn.execute("SELECT customer_name, ledger_type FROM customers WHERE company_name=?", (COMPANY,)):
    name=(r["customer_name"] or "").strip()
    if name:
        ledger_meta[name.lower()]=(r["ledger_type"] or "").strip()

CASH_TYPES={"cash-in-hand","bank accounts","bank od a/c","bank od accounts"}

def is_cash(name):
    lt=ledger_meta.get(name.lower(),"").lower()
    return lt in CASH_TYPES

def category(name):
    lt=ledger_meta.get(name.lower(),"").strip()
    if not lt:
        return "Others"
    return TO_PRIMARY.get(lt.lower(), lt)

xml=f"""
<ENVELOPE>
 <HEADER><VERSION>1</VERSION><TALLYREQUEST>Export</TALLYREQUEST><TYPE>Collection</TYPE><ID>CFV</ID></HEADER>
 <BODY><DESC>
  <STATICVARIABLES>
   <SVFROMDATE TYPE="Date">20260401</SVFROMDATE>
   <SVTODATE TYPE="Date">20260430</SVTODATE>
   <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
   <SVCURRENTCOMPANY>{escape(COMPANY)}</SVCURRENTCOMPANY>
  </STATICVARIABLES>
  <TDL><TDLMESSAGE>
   <COLLECTION NAME="CFV">
    <TYPE>Voucher</TYPE>
    <FETCH>DATE, GUID, VOUCHERNUMBER, VOUCHERTYPENAME, ISDELETED, ISCANCELLED, ISOPTIONAL, ALLLEDGERENTRIES.LIST, LEDGERENTRIES.LIST</FETCH>
   </COLLECTION>
  </TDLMESSAGE></TDL>
 </DESC></BODY>
</ENVELOPE>"""
print("fetching...")
r=requests.post(TALLY,data=xml.encode(),headers={"Content-Type":"application/xml","Connection":"close"},timeout=180)
text=clean_source_xml(decode_response(r))
print("bytes", len(text))
data=xmltodict.parse(text)
coll=((data.get("ENVELOPE") or {}).get("BODY") or {}).get("DATA") or {}
c=coll.get("COLLECTION") or coll
vouchers=c.get("VOUCHER") or []
if isinstance(vouchers, dict): vouchers=[vouchers]
print("vouchers", len(vouchers))

def entries(v):
    raw=v.get("ALLLEDGERENTRIES.LIST") or v.get("LEDGERENTRIES.LIST") or []
    if isinstance(raw, dict): raw=[raw]
    out=[]
    for e in raw:
        if not isinstance(e, dict): continue
        nm=get_text(e.get("LEDGERNAME"))
        if not nm: continue
        out.append({"name":nm,"amount":safe_float(e.get("AMOUNT")),"deemed":get_text(e.get("ISDEEMEDPOSITIVE")).lower()=="yes"})
    return out

inflow=defaultdict(float)
outflow=defaultdict(float)
cash_in=cash_out=0.0
n=0
# debug first few cash vouchers
dbg=0
for v in vouchers:
    if get_text(v.get("ISDELETED"))=="Yes" or get_text(v.get("ISCANCELLED"))=="Yes" or get_text(v.get("ISOPTIONAL"))=="Yes":
        continue
    ents=entries(v)
    cash=[e for e in ents if is_cash(e["name"])]
    others=[e for e in ents if not is_cash(e["name"])]
    if not cash: continue
    n+=1
    # Tally XML: AMOUNT typically negative for debit
    net_debit = sum(-e["amount"] for e in cash)  # >0 means cash increased (inflow)
    if dbg < 3:
        print("DBG", get_text(v.get("VOUCHERTYPENAME")), get_text(v.get("VOUCHERNUMBER")), "cash", cash, "others", [(e["name"], e["amount"], category(e["name"])) for e in others[:5]], "net_debit", net_debit)
        dbg+=1
    if abs(net_debit) < 1e-6: continue
    if net_debit > 0:
        cash_in += net_debit
        pool=[e for e in others if e["amount"] > 0] or others  # credits
        tot=sum(abs(e["amount"]) for e in pool) or 1.0
        for e in pool:
            inflow[category(e["name"])] += net_debit * abs(e["amount"]) / tot
    else:
        amt=-net_debit
        cash_out += amt
        pool=[e for e in others if e["amount"] < 0] or others  # debits
        tot=sum(abs(e["amount"]) for e in pool) or 1.0
        for e in pool:
            outflow[category(e["name"])] += amt * abs(e["amount"]) / tot

print("cash vouchers", n)
print("IN", round(cash_in,2), "OUT", round(cash_out,2), "NETT", round(cash_in-cash_out,2))
print("Expected IN 698515.44 OUT 272566.01 NETT 425949.43")
print("INFLOW cats:")
for k,v in sorted(inflow.items(), key=lambda x:-x[1]):
    print(f"  {k}: {v:,.2f}")
print("OUTFLOW cats:")
for k,v in sorted(outflow.items(), key=lambda x:-x[1]):
    print(f"  {k}: {v:,.2f}")
print("Expected IN: CL 1890, CA 696622.81, II 2.63")
print("Expected OUT: CL 59476.21, CA 1545.50, DE 500, II 0.52, IE 211043.78")
