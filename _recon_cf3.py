import requests
from xml.sax.saxutils import escape
from collections import defaultdict
import sqlite3
from src.source_values import clean_source_xml, decode_response
import xmltodict

TALLY="http://127.0.0.1:9000"
COMPANY="AL ZAHRA DEMO EQIPMENT"
TO_PRIMARY = {
    "cash-in-hand": "Current Assets", "bank accounts": "Current Assets", "bank od a/c": "Current Assets",
    "bank od accounts": "Current Assets", "stock-in-hand": "Current Assets", "sundry debtors": "Current Assets",
    "deposits (asset)": "Current Assets", "loans & advances (asset)": "Current Assets", "current assets": "Current Assets",
    "sundry creditors": "Current Liabilities", "duties & taxes": "Current Liabilities", "provisions": "Current Liabilities",
    "current liabilities": "Current Liabilities", "duties and taxes": "Current Liabilities",
    "indirect expenses": "Indirect Expenses", "indirect incomes": "Indirect Incomes",
    "direct expenses": "Direct Expenses", "direct incomes": "Direct Incomes",
    "sales accounts": "Sales Accounts", "purchase accounts": "Purchase Accounts",
    "fixed assets": "Fixed Assets", "capital account": "Capital Account",
    "loans (liability)": "Loans (Liability)", "secured loans": "Loans (Liability)", "unsecured loans": "Loans (Liability)",
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
    if name: ledger_meta[name.lower()]=(r["ledger_type"] or "").strip()
CASH_TYPES={"cash-in-hand","bank accounts","bank od a/c","bank od accounts"}
def is_cash(name): return ledger_meta.get(name.lower(),"").lower() in CASH_TYPES
def category(name):
    lt=ledger_meta.get(name.lower(),"").strip()
    return TO_PRIMARY.get(lt.lower(), lt or "Others")

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
   <COLLECTION NAME="CFV"><TYPE>Voucher</TYPE>
    <FETCH>DATE, GUID, VOUCHERNUMBER, VOUCHERTYPENAME, ISDELETED, ISCANCELLED, ISOPTIONAL, ALLLEDGERENTRIES.LIST, LEDGERENTRIES.LIST</FETCH>
   </COLLECTION>
  </TDLMESSAGE></TDL>
 </DESC></BODY>
</ENVELOPE>"""
r=requests.post(TALLY,data=xml.encode(),headers={"Content-Type":"application/xml","Connection":"close"},timeout=180)
text=clean_source_xml(decode_response(r))
data=xmltodict.parse(text)
c=((data.get("ENVELOPE") or {}).get("BODY") or {}).get("DATA") or {}
c=c.get("COLLECTION") or c
vouchers=c.get("VOUCHER") or []
if isinstance(vouchers, dict): vouchers=[vouchers]

# Find Direct Expenses / Indirect Incomes contributions
by_ledger_in=defaultdict(float)
by_ledger_out=defaultdict(float)
by_type_in=defaultdict(float)
by_type_out=defaultdict(float)

def entries(v):
    raw=v.get("ALLLEDGERENTRIES.LIST") or v.get("LEDGERENTRIES.LIST") or []
    if isinstance(raw, dict): raw=[raw]
    out=[]
    for e in raw:
        if not isinstance(e, dict): continue
        nm=get_text(e.get("LEDGERNAME"))
        if nm: out.append({"name":nm,"amount":safe_float(e.get("AMOUNT"))})
    return out

for v in vouchers:
    if get_text(v.get("ISDELETED"))=="Yes" or get_text(v.get("ISCANCELLED"))=="Yes" or get_text(v.get("ISOPTIONAL"))=="Yes":
        continue
    ents=entries(v)
    cash=[e for e in ents if is_cash(e["name"])]
    others=[e for e in ents if not is_cash(e["name"])]
    if not cash: continue
    net_debit=sum(-e["amount"] for e in cash)
    if abs(net_debit)<1e-6: continue
    if net_debit>0:
        pool=[e for e in others if e["amount"]>0] or others
        tot=sum(abs(e["amount"]) for e in pool) or 1.0
        for e in pool:
            share=net_debit*abs(e["amount"])/tot
            by_ledger_in[e["name"]]+=share
            by_type_in[ledger_meta.get(e["name"].lower(),"?")]+=share
    else:
        amt=-net_debit
        pool=[e for e in others if e["amount"]<0] or others
        tot=sum(abs(e["amount"]) for e in pool) or 1.0
        for e in pool:
            share=amt*abs(e["amount"])/tot
            by_ledger_out[e["name"]]+=share
            by_type_out[ledger_meta.get(e["name"].lower(),"?")]+=share

print("IN by ledger_type:")
for k,v in sorted(by_type_in.items(), key=lambda x:-x[1]):
    print(f"  {k!r}: {v:,.2f}")
print("OUT by ledger_type:")
for k,v in sorted(by_type_out.items(), key=lambda x:-x[1]):
    print(f"  {k!r}: {v:,.2f}")

print("\nOUT ledgers near 500:")
for k,v in sorted(by_ledger_out.items(), key=lambda x:-x[1]):
    if 400 < v < 600 or "direct" in k.lower() or "freight" in k.lower() or "expense" in ledger_meta.get(k.lower(),"").lower():
        if v>100:
            print(f"  {k} [{ledger_meta.get(k.lower())}] {v:,.2f}")

print("\nIN small amounts (possible II 2.63):")
for k,v in sorted(by_ledger_in.items(), key=lambda x:x[1]):
    if 0 < v < 20:
        print(f"  {k} [{ledger_meta.get(k.lower())}] {v:,.2f}")

# Also try: include ALL others not just credit/debit pool - use absolute proportional of all others
print("\n--- alternate: allocate to all others by abs amount ---")
inflow=defaultdict(float); outflow=defaultdict(float); cin=cout=0
for v in vouchers:
    if get_text(v.get("ISDELETED"))=="Yes" or get_text(v.get("ISCANCELLED"))=="Yes" or get_text(v.get("ISOPTIONAL"))=="Yes":
        continue
    ents=entries(v)
    cash=[e for e in ents if is_cash(e["name"])]
    others=[e for e in ents if not is_cash(e["name"])]
    if not cash or not others: continue
    net_debit=sum(-e["amount"] for e in cash)
    if abs(net_debit)<1e-6: continue
    tot=sum(abs(e["amount"]) for e in others) or 1.0
    if net_debit>0:
        cin+=net_debit
        for e in others:
            inflow[category(e["name"])]+=net_debit*abs(e["amount"])/tot
    else:
        amt=-net_debit; cout+=amt
        for e in others:
            outflow[category(e["name"])]+=amt*abs(e["amount"])/tot
print("IN",round(cin,2),"OUT",round(cout,2))
for label, d in [("IN",inflow),("OUT",outflow)]:
    print(label)
    for k,v in sorted(d.items(), key=lambda x:-x[1]):
        print(f"  {k}: {v:,.2f}")
