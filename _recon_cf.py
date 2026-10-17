import requests, re, xmltodict
from xml.sax.saxutils import escape
from collections import defaultdict
import sqlite3
from pathlib import Path

TALLY="http://127.0.0.1:9000"
COMPANY="AL ZAHRA DEMO EQIPMENT"

def get_text(v):
    if v is None: return ""
    if isinstance(v, dict): return str(v.get("#text") or v.get("text") or "").strip()
    return str(v).strip()

def safe_float(v):
    try:
        s=get_text(v).replace(",","")
        if not s: return 0.0
        return float(s)
    except: return 0.0

# ledger -> primary-ish group from local customers
conn=sqlite3.connect("data/sync_cache.db")
conn.row_factory=sqlite3.Row
ccols=[r[1] for r in conn.execute("PRAGMA table_info(customers)").fetchall()]
print("customer cols", ccols)
# pick useful
rows=conn.execute("SELECT * FROM customers WHERE company_name=? LIMIT 3", (COMPANY,)).fetchall()
if rows:
    print("sample", dict(rows[0]))

# map name -> ledger_type / parent
ledger_meta={}
q=f"SELECT * FROM customers WHERE company_name=?"
for r in conn.execute(q,(COMPANY,)):
    d=dict(r)
    name=(d.get("customer_name") or d.get("name") or "").strip()
    if not name: continue
    ledger_meta[name.lower()]={
        "name": name,
        "ledger_type": (d.get("ledger_type") or "").strip(),
        "parent": (d.get("parent") or d.get("group_name") or d.get("parent_name") or "").strip(),
    }
print("ledger_meta", len(ledger_meta))
# show cash/bank
for k,v in list(ledger_meta.items()):
    lt=(v["ledger_type"]+" "+v["parent"]).lower()
    if "cash" in lt or "bank" in lt:
        print("CASHLIKE", v)

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
print("fetching vouchers...")
r=requests.post(TALLY,data=xml.encode(),headers={"Content-Type":"application/xml","Connection":"close"},timeout=180)
text=r.content.decode("utf-8","replace")
print("bytes", len(text))
data=xmltodict.parse(text)
coll=((data.get("ENVELOPE") or {}).get("BODY") or {}).get("DATA") or {}
# collection may be under COLLECTION
c=coll.get("COLLECTION") or coll
vouchers=c.get("VOUCHER") or []
if isinstance(vouchers, dict): vouchers=[vouchers]
print("vouchers", len(vouchers))

CASH_HINTS=("cash-in-hand","cash in hand","bank accounts","bank account","od accounts","overdraft")

def is_cash_ledger(name):
    meta=ledger_meta.get(name.lower())
    if not meta: 
        # name heuristics
        n=name.lower()
        return any(x in n for x in ("cash","bank","petty"))
    blob=(meta["ledger_type"]+" "+meta["parent"]).lower()
    return any(h in blob for h in CASH_HINTS) or any(h in name.lower() for h in ("cash","bank"))

def primary_group(name):
    meta=ledger_meta.get(name.lower()) or {}
    # Prefer ledger_type if it looks like a Tally reserved group
    lt=meta.get("ledger_type") or ""
    parent=meta.get("parent") or ""
    # Often ledger_type is like "Sundry Debtors" under Current Assets - we need PRIMARY
    # customers may store primary in ledger_type for some
    for cand in (lt, parent):
        cl=cand.lower()
        for p in ["Current Assets","Current Liabilities","Indirect Expenses","Indirect Incomes","Direct Expenses","Direct Incomes","Fixed Assets","Capital Account","Sales Accounts","Purchase Accounts","Suspense","Loans (Liability)","Investments"]:
            if p.lower() in cl or cl==p.lower():
                return p
    return lt or parent or "Others"

def ledger_entries(v):
    raw=v.get("ALLLEDGERENTRIES.LIST") or v.get("LEDGERENTRIES.LIST") or []
    if isinstance(raw, dict): raw=[raw]
    out=[]
    for e in raw:
        if not isinstance(e, dict): continue
        nm=get_text(e.get("LEDGERNAME"))
        amt=safe_float(e.get("AMOUNT"))
        # Tally: negative amount often credit depending on export; check ISDEEMEDPOSITIVE
        is_deemed=get_text(e.get("ISDEEMEDPOSITIVE")).lower()=="yes"
        # Standard: AMOUNT signed; debit positive when ISDEEMEDPOSITIVE Yes for that ledger nature
        out.append({"name":nm,"amount":amt,"is_deemed":is_deemed,"raw":e})
    return out

inflow=defaultdict(float)
outflow=defaultdict(float)
cash_in_total=0.0
cash_out_total=0.0
v_with_cash=0

for v in vouchers:
    if get_text(v.get("ISDELETED"))=="Yes" or get_text(v.get("ISCANCELLED"))=="Yes":
        continue
    if get_text(v.get("ISOPTIONAL"))=="Yes":
        continue
    entries=ledger_entries(v)
    if not entries: continue
    cash=[e for e in entries if is_cash_ledger(e["name"])]
    others=[e for e in entries if not is_cash_ledger(e["name"])]
    if not cash: continue
    v_with_cash+=1
    # Net cash movement: sum of cash amounts. In Tally voucher XML, AMOUNT is typically signed
    # with Dr as negative in some exports. Check distribution.
    # Heuristic used by many: for cash ledger, debit (money in) when amount is such that...
    # Look at ISDEEMEDPOSITIVE: for assets, Yes means debit.
    net_cash_debit=0.0
    for e in cash:
        # Asset ledgers: ISDEEMEDPOSITIVE Yes => debit increases asset (inflow)
        if e["is_deemed"]:
            # amount often stored as negative for debit in Tally XML for assets
            # Use signed amount convention: if ISDEEMEDPOSITIVE, -amount is debit?
            amt=e["amount"]
            # Common Tally: AMOUNT is negative for debit entries
            # Debit to cash (inflow) => negative amount on cash line
            net_cash_debit += (-amt)  # convert to positive debit
        else:
            amt=e["amount"]
            net_cash_debit += (-amt)
    # Simplify: treat sum(-amount) on cash as net debit to cash (inflow if >0)
    net = sum(-e["amount"] for e in cash)
    if abs(net) < 0.0001:
        continue
    if net > 0:
        cash_in_total += net
        # allocate to credit-side others (negative debit = credit)
        credits=[e for e in others if (-e["amount"]) < 0 or e["amount"]>0]
        # others with positive AMOUNT are typically credits
        pool=[e for e in others if e["amount"] > 0] or others
        total_pool=sum(abs(e["amount"]) for e in pool) or 1.0
        for e in pool:
            share=net * (abs(e["amount"])/total_pool)
            inflow[primary_group(e["name"])] += share
    else:
        out_amt=-net
        cash_out_total += out_amt
        pool=[e for e in others if e["amount"] < 0] or others
        total_pool=sum(abs(e["amount"]) for e in pool) or 1.0
        for e in pool:
            share=out_amt * (abs(e["amount"])/total_pool)
            outflow[primary_group(e["name"])] += share

print("v_with_cash", v_with_cash)
print("cash_in_total", round(cash_in_total,2), "expected 698515.44")
print("cash_out_total", round(cash_out_total,2), "expected 272566.01")
print("INFLOW:")
for k,v in sorted(inflow.items(), key=lambda x:-x[1]):
    print(f"  {k}: {v:,.2f}")
print("OUTFLOW:")
for k,v in sorted(outflow.items(), key=lambda x:-x[1]):
    print(f"  {k}: {v:,.2f}")
