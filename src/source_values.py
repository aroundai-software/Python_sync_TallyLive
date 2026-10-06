"""Native Tally values shared by the connector's read adapters."""
from datetime import datetime
from decimal import Decimal
import os,re

def text(value):
    if isinstance(value,list):return text(value[0]) if value else ''
    if isinstance(value,dict):return text(value.get('#text',''))
    return str(value or '').strip()

def source_number(value, default=0):
    raw=text(value).replace(',','').strip()
    if not raw:return default
    # Tally supplies the exact converted base value after '='. Never treat
    # the foreign amount or its exchange rate as an INR amount.
    if '=' in raw:
        raw=raw.rsplit('=',1)[1].strip()
        if not ('₹' in raw or re.search(r'\bINR\b|\bRs\.?',raw)):
            raise ValueError('Converted value is not explicitly denominated in INR')
    elif any(symbol in raw for symbol in ('$','€','£','\u20c1','\u20c3')) or re.search(r'\b(?:USD|AED|BD|BHD|SAR|EUR|GBP)\b',raw):
        raise ValueError('Foreign amount has no native INR conversion')
    match=re.match(r'\s*([+-]?)\s*(?:₹|INR\s*|Rs\.?\s*)?\s*([+-]?\d+(?:\.\d+)?)',raw)
    if not match:raise ValueError('Invalid Tally numeric value')
    number=Decimal(match[2])
    if match[1]=='-':number=-abs(number)
    if raw.lower().endswith(' dr'):number=-abs(number)
    elif raw.lower().endswith(' cr'):number=abs(number)
    return float(number)

def decode_response(response):
    raw=response.content
    if raw.startswith((b'\xff\xfe',b'\xfe\xff')):return raw.decode('utf-16')
    if raw[:2]==b'<\x00':return raw.decode('utf-16-le')
    if raw[:2]==b'\x00<':return raw.decode('utf-16-be')
    return raw.decode('utf-8-sig')

def clean_source_xml(raw):
    def reference(match):
        code=int(match[0][3:-1],16) if match[0].startswith('&#x') else int(match[0][2:-1])
        return '' if code<32 and code not in (9,10,13) else match[0]
    raw=re.sub(r'&#(?:x[0-9A-Fa-f]+|\d+);',reference,raw)
    raw=re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]','',raw)
    raw=re.sub(r'&(?!amp;|lt;|gt;|quot;|apos;|#\d+;|#x[0-9A-Fa-f]+;)','&amp;',raw)
    if '<UDF:' in raw and 'xmlns:UDF=' not in raw:raw=raw.replace('<ENVELOPE>','<ENVELOPE xmlns:UDF="TallyUDF">',1)
    return raw

def gst_names_xml(xml):
    from xml.etree import ElementTree as ET
    root=ET.fromstring(clean_source_xml(xml))
    if list(root.iter('LINEERROR')) or list(root.iter('ERROR')):raise ValueError('Tally tax-ledger export failed')
    if any(text(e.text)!='1' for e in root.findall('./HEADER/STATUS')):raise ValueError('Unsuccessful native tax-ledger status')
    return {(r.get('NAME') or r.findtext('NAME') or '').strip().casefold() for r in root.iter('LEDGER') if r.findtext('TAXTYPE')=='GST'}

def effective_xml(parent,tag):
    today=datetime.now().strftime('%Y%m%d')
    rows=[e for e in parent.findall(tag) if (e.findtext('APPLICABLEFROM') or '00000000')<=today]
    return max(rows,key=lambda e:e.findtext('APPLICABLEFROM') or '',default=None)

def effective_dict(rows):
    if isinstance(rows,dict):rows=[rows]
    today=datetime.now().strftime('%Y%m%d')
    rows=[e for e in (rows or []) if isinstance(e,dict) and text(e.get('APPLICABLEFROM'))<=today]
    return max(rows,key=lambda e:text(e.get('APPLICABLEFROM')),default={})

def ledger_entries(voucher):
    for key in ('ALLLEDGERENTRIES.LIST','LEDGERENTRIES.LIST'):
        rows=voucher.get(key)
        if isinstance(rows,dict):rows=[rows]
        rows=[e for e in rows if isinstance(e,dict)] if isinstance(rows,list) else []
        if rows:return rows
    return []

def invoice_tax(voucher, names, purchase=False):
    normalized={text(n).casefold() for n in names}
    amount=sum(source_number(e.get('AMOUNT')) for e in ledger_entries(voucher) if text(e.get('LEDGERNAME')).casefold() in normalized)
    return round(-amount if purchase else amount,2)

def inventory_tax(entry, quantity, unit_price):
    rates=entry.get('RATEDETAILS.LIST') or []
    if isinstance(rates,dict):rates=[rates]
    heads={text(r.get('GSTRATEDUTYHEAD')).casefold():source_number(r.get('GSTRATE')) for r in rates if isinstance(r,dict)}
    rate=heads.get('igst',heads.get('integrated tax',0))
    if not rate:rate=heads.get('cgst',heads.get('central tax',0))+heads.get('sgst/utgst',heads.get('state tax',0))
    if not rate:
        overrides=entry.get('GSTOVRDNLEDGERS.LIST') or []
        if isinstance(overrides,dict):overrides=[overrides]
        rate=sum(source_number(r.get('TAXRATE')) for r in overrides if isinstance(r,dict))
    # Inventory AMOUNT is already net of discount, before voucher GST.
    net=abs(source_number(entry.get('AMOUNT'))) if text(entry.get('AMOUNT')) else quantity*unit_price
    return round(rate,2),round(net*rate/100,2)

def paged_rows(client,table,columns,company):
    result=[]
    for offset in range(0,1000000,1000):
        rows=client.table(table).select(columns).eq('company_name',company).order('id').range(offset,offset+999).execute().data or []
        result.extend(rows)
        if len(rows)<1000:return result
    raise RuntimeError('Unexpected pagination limit')

def reconcile_from_id(value):
    return 0 if os.environ.get('FULL_RECONCILE','').lower() in ('1','true','yes') else value

def company_currency(company):
    raw=text(company.get('BASECURRENCYNAME') or company.get('CURRENCYNAME') or os.environ.get('TALLY_BASE_CURRENCY'))
    mapping={'₹':'INR','Rs':'INR','Rs.':'INR','INR':'INR','AED':'AED','Dhs':'AED','USD':'USD','$':'USD','EUR':'EUR','€':'EUR','GBP':'GBP','£':'GBP'}
    if raw not in mapping:raise ValueError('Native company base currency is unavailable or unsupported')
    return mapping[raw]
