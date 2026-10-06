"""Offline regressions using the actual source-value shapes from this audit."""
import ast,os,sys,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from xml.etree import ElementTree as ET
from datetime import datetime,timezone,timedelta
import re
sys.path.insert(0,str(Path(__file__).resolve().parent))
from src.source_values import *

def functions(module,names,globals_extra=None):
    path=Path(__file__).resolve().parent/'src'/module
    tree=ast.parse(path.read_text(encoding='utf-8'))
    tree.body=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in names]
    scope={**globals(),**(globals_extra or {})}
    exec(compile(tree,str(path),'exec'),scope)
    return scope

class ConnectorRegressionTests(unittest.TestCase):
    def test_foreign_sales_converted_inr(self):
        self.assertEqual(source_number('$395.00 @ ₹ 95.6905/$ = ₹ 37797.75'),37797.75)
        self.assertEqual(source_number('-$395.00 @ ₹ 95.6905/$ = -₹ 37797.75'),-37797.75)
    def test_purchase_currency_conversion(self):
        self.assertEqual(source_number('1200.00 AED @ ₹ 26/AED = ₹ 31200.00'),31200)
    def test_no_guessed_foreign_value(self):
        with self.assertRaises(ValueError):source_number('$395.00')
    def test_empty_missing_is_not_fabricated(self):
        self.assertIsNone(source_number('',None))
    def test_native_credit_debit_suffixes(self):
        self.assertEqual(source_number('1,327.94 Cr'),1327.94)
        self.assertEqual(source_number('82.00 Dr'),-82)
    def test_unicode_and_valid_xml_references(self):
        value=decode_response(SimpleNamespace(content='<ENVELOPE><NAME>St. Joseph’s Convent</NAME></ENVELOPE>'.encode('utf-16')))
        self.assertIn('Joseph’s',value)
        self.assertEqual(ET.fromstring(clean_source_xml('<NAME>A&#8217;B&#4;</NAME>')).text,'A’B')
    def test_company_currency_is_native(self):
        self.assertEqual(company_currency({'BASECURRENCYNAME':'₹'}),'INR')
        with patch.dict(os.environ,{'TALLY_BASE_CURRENCY':''}):
            # Tally's own value still wins over the baked-in default.
            self.assertEqual(company_currency({'BASECURRENCYNAME':'AED'}),'AED')
            # Omitted currency falls back to the hardcoded default, not to AED.
            self.assertEqual(company_currency({}),'INR')
            # An unrecognised currency is still refused rather than mislabelled.
            with self.assertRaises(ValueError):company_currency({'BASECURRENCYNAME':'XYZ'})
    def test_explicit_currency_when_native_export_omits_it(self):
        with patch.dict(os.environ,{'TALLY_BASE_CURRENCY':'INR'}):self.assertEqual(company_currency({}),'INR')
    def test_sales_gst_from_all_ledger_entries(self):
        voucher={'ALLLEDGERENTRIES.LIST':[{'LEDGERNAME':'Zee7inc','AMOUNT':'-5310.00'},{'LEDGERNAME':'Sales Gst','AMOUNT':'4500.00'},{'LEDGERNAME':'Central Tax','AMOUNT':'405.00'},{'LEDGERNAME':'State Tax','AMOUNT':'405.00'}]}
        self.assertEqual(invoice_tax(voucher,{'central tax','state tax'}),810)
    def test_purchase_gst_retains_sign(self):
        self.assertEqual(invoice_tax({'LEDGERENTRIES.LIST':{'LEDGERNAME':'Central Tax','AMOUNT':'-270.00'}},{'central tax'},True),270)
    def test_tax_ledger_discovery_by_native_type(self):
        xml='<ENVELOPE><COLLECTION><LEDGER NAME="Central Tax"><TAXTYPE>GST</TAXTYPE></LEDGER><LEDGER NAME="Tds"><TAXTYPE>TDS</TAXTYPE></LEDGER></COLLECTION></ENVELOPE>'
        self.assertEqual(gst_names_xml(xml),{'central tax'})
    def test_inventory_gst_uses_native_net_value(self):
        entry={'AMOUNT':'900.00','RATEDETAILS.LIST':[{'GSTRATEDUTYHEAD':'IGST','GSTRATE':'18'}]}
        self.assertEqual(inventory_tax(entry,1,1000),(18,162))
    def test_daybook_sign_follows_accounting_amount(self):
        scope=functions('daybook.py',{'signed_entry_amount'})
        self.assertEqual(scope['signed_entry_amount']({'AMOUNT':'-0.30','ISDEEMEDPOSITIVE':'No'}),-0.3)
    def test_effective_details_exclude_future_values(self):
        rows=[{'APPLICABLEFROM':'20220401','HSNCODE':'85238020'},{'APPLICABLEFROM':'20990101','HSNCODE':'future'}]
        self.assertEqual(effective_dict(rows)['HSNCODE'],'85238020')
    def test_effective_mailing_xml(self):
        xml=ET.fromstring('<LEDGER><LEDMAILINGDETAILS.LIST><APPLICABLEFROM>20220401</APPLICABLEFROM><COUNTRY>India</COUNTRY></LEDMAILINGDETAILS.LIST><LEDMAILINGDETAILS.LIST><APPLICABLEFROM>20990101</APPLICABLEFROM><COUNTRY>Future</COUNTRY></LEDMAILINGDETAILS.LIST></LEDGER>')
        self.assertEqual(effective_xml(xml,'LEDMAILINGDETAILS.LIST').findtext('COUNTRY'),'India')
    def test_absolute_due_date_before_duration_digits(self):
        scope=functions('outstanding_payables.py',{'get_text','clean_text','format_tally_date','julian_to_date','resolve_due_date_and_credit_days'})
        due,days,_=scope['resolve_due_date_and_credit_days']({'BILLCREDITPERIOD':'12-Apr-20'},'2020-03-12')
        self.assertEqual((due,days),('2020-04-12',31))
    def test_reconcile_ignores_global_high_water_mark(self):
        with patch.dict(os.environ,{'FULL_RECONCILE':'1'}):self.assertEqual(reconcile_from_id(176999),0)
    def test_balance_lookup_paginates_beyond_1000(self):
        class Client:
            def table(self,*a):return self
            def select(self,*a):return self
            def eq(self,*a):return self
            def order(self,*a):return self
            def range(self,start,end):self.start=start;return self
            def execute(self):return SimpleNamespace(data=[{'id':i} for i in range(self.start,min(self.start+1000,7684))])
        self.assertEqual(len(paged_rows(Client(),'customers','id','company')),7684)

if __name__=='__main__':unittest.main()
