"""Native Tally values shared by the connector's read adapters."""
from datetime import datetime
from decimal import Decimal
import os,re

def text(value):
    if isinstance(value,list):return text(value[0]) if value else ''
    if isinstance(value,dict):return text(value.get('#text',''))
    return str(value or '').strip()

_FOREIGN_CURRENCY_RE=re.compile(
    r'\b(?:USD|AED|BD|BHD|SAR|EUR|GBP|ZAR|BWP|QAR|OMR|KWD|CHF|JPY|CNY|SGD|AUD|CAD|NZD)\b',
    re.I,
)
_FOREIGN_CURRENCY_SYMBOLS=('$','€','£','\u20c1','\u20c3')
_NUMBER_RE=re.compile(
    r'\s*([+-]?)\s*(?:₹|INR\s*|Rs\.?\s*)?\s*([+-]?\d+(?:\.\d+)?)',
    re.I,
)
_LOOSE_NUMBER_RE=re.compile(r'([+-]?)\s*(\d+(?:\.\d+)?)')

def _has_foreign_currency(raw):
    return any(symbol in raw for symbol in _FOREIGN_CURRENCY_SYMBOLS) or bool(_FOREIGN_CURRENCY_RE.search(raw))

def _has_inr_denomination(raw):
    return '₹' in raw or bool(re.search(r'\bINR\b|\bRs\.?',raw,re.I))

def _finalize_number(raw, sign_prefix, digits):
    number=Decimal(digits)
    if sign_prefix=='-':number=-abs(number)
    low=raw.lower()
    if low.endswith(' dr') or low.rstrip().endswith('dr'):number=-abs(number)
    elif low.endswith(' cr') or low.rstrip().endswith('cr'):number=abs(number)
    return float(number)

def source_number(value, default=0):
    raw=text(value).replace(',','').strip()
    if not raw:return default
    # Tally uses '=' for forex ("$10 = ₹830" or "$10 = 830") and stock units
    # ("10 Nos = 100.5 kg"). Prefer the value after '='.
    if '=' in raw:
        left,right=raw.rsplit('=',1)
        right=right.strip()
        # Many multi-currency companies omit ₹/INR on the converted side.
        # If the right side still has a parseable number, accept it.
        if (_has_foreign_currency(left) or _has_foreign_currency(right)) and not _has_inr_denomination(right):
            if not _LOOSE_NUMBER_RE.search(right):
                raise ValueError('Converted value is not explicitly denominated in INR')
        raw=right
    elif _has_foreign_currency(raw):
        raise ValueError('Foreign amount has no native INR conversion')
    match=_NUMBER_RE.match(raw)
    if match:
        return _finalize_number(raw, match[1], match[2])
    loose=_LOOSE_NUMBER_RE.search(raw)
    if loose:
        return _finalize_number(raw, loose[1], loose[2])
    raise ValueError('Invalid Tally numeric value')

def decode_response(response):
    raw=response.content
    if raw.startswith((b'\xff\xfe',b'\xfe\xff')):return raw.decode('utf-16')
    if raw[:2]==b'<\x00':return raw.decode('utf-16-le')
    if raw[:2]==b'\x00<':return raw.decode('utf-16-be')
    # Indian Windows Tally often emits 0xA0 (NBSP) in UTF-8 exports.
    try:
        return raw.decode('utf-8-sig')
    except UnicodeDecodeError:
        return raw.decode('cp1252')

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

# Used only when Tally's export omits the base currency entirely. Tally's own
# value always wins, so this is not the blanket override the old AED constant was.
DEFAULT_BASE_CURRENCY='INR'

# Tally reports the base currency as a symbol, and only over a UTF-16 request;
# a UTF-8 request flattens every symbol to '?'. The Currency collection's
# mailing name is the reliable source, so both spellings are mapped here.
CURRENCY_NAMES={
 '₹':'INR','\u20b9':'INR','Rs':'INR','Rs.':'INR','INR':'INR','RUPEES':'INR','INDIAN RUPEES':'INR',
 '\u20c3':'AED','AED':'AED','DHS':'AED','DH':'AED','UAE DIRHAM':'AED','DIRHAM':'AED','DIRHAMS':'AED',
 '$':'USD','USD':'USD','US DOLLAR':'USD','€':'EUR','EUR':'EUR','EURO':'EUR',
 '£':'GBP','GBP':'GBP','POUND':'GBP','POUND STERLING':'GBP',
 'SAR':'SAR','SAUDI RIYAL':'SAR','BHD':'BHD','BAHRAINI DINAR':'BHD',
 'OMR':'OMR','QAR':'QAR','KWD':'KWD',
}

def currency_code(raw):
    """Map a Tally currency symbol or name onto an ISO code, or None."""
    raw=text(raw)
    return CURRENCY_NAMES.get(raw) or CURRENCY_NAMES.get(raw.upper())

def company_currency(company):
    for key in ('BASECURRENCYMAILINGNAME','BASECURRENCYNAME','CURRENCYNAME'):
        code=currency_code(company.get(key))
        if code:return code
    # A '?' here means the request went out as UTF-8 and Tally could not encode
    # the symbol; fall back rather than mislabel, but never silently guess a
    # currency when Tally gave us a real value we simply do not recognise.
    for key in ('BASECURRENCYMAILINGNAME','BASECURRENCYNAME','CURRENCYNAME'):
        raw=text(company.get(key))
        if raw and raw!='?':raise ValueError(f'Unsupported Tally base currency {raw!r}')
    code=currency_code(os.environ.get('TALLY_BASE_CURRENCY')) or currency_code(DEFAULT_BASE_CURRENCY)
    return code
