import os
import logging
from supabase import create_client, Client
from src.config import load_config

config_data = load_config()
SUPABASE_URL = config_data.get("supabase_url")
SUPABASE_KEY = config_data.get("supabase_key")

if not SUPABASE_URL or not SUPABASE_KEY:
    logging.warning("Supabase URL or Key is missing. Sync will not work until configured.")
    supabase: Client = None
else:
    supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

def sync_companies(companies):
    if not supabase or not companies: return None
    try:
        formatted_data = []
        for c in companies:
            if not c: continue
            
            # Handle address parsing from Tally (often a single string or list)
            address_raw = c.get("ADDRESS")
            address_line1 = ""
            address_line2 = ""
            if isinstance(address_raw, list) and len(address_raw) > 0:
                address_line1 = address_raw[0]
                if len(address_raw) > 1:
                    address_line2 = ", ".join(address_raw[1:])
            elif isinstance(address_raw, str):
                address_line1 = address_raw

            formatted_data.append({
                "company_name": c.get("NAME", "Unknown"),
                "company_number": c.get("COMPANYNUMBER", "1"),
                "Guid": c.get("GUID", ""),
                "state": c.get("STATENAME"),
                "pin_code": c.get("PINCODE"),
                "email": c.get("EMAIL"),
                "phone_number": c.get("PHONENUMBER"),
                "pan_number": c.get("INCOMETAXNUMBER"),
                "address_line1": address_line1,
                "address_line2": address_line2,
                "is_active": True
            })

        if formatted_data:
            res = supabase.table("tally_companies").upsert(formatted_data, on_conflict="Guid").execute()
            logging.info(f"Successfully synced {len(formatted_data)} companies.")
            return res.data[0]["id"] if res.data else None
    except Exception as e:
        logging.error(f"Error syncing companies: {e}")
    return None

def sync_products(products, company_id, company_name):
    if not supabase or not products: return
    try:
        formatted_data = []
        for p in products:
            if not p: continue
            rate = p.get('RATEOFVAT', '0')
            try: rate = float(rate)
            except: rate = 0.0

            formatted_data.append({
                "ItemName": p.get("NAME"),
                "Guid": p.get("GUID"),
                "ItemUnit": p.get("BASEUNITS", "PCS"),
                "GstRate": rate,
                "PartNumber": p.get("PARTNO"),
                "description": p.get("DESCRIPTION"),
                "company_id": company_id,
                "company_name": company_name
            })
        
        # Batch upsert
        res = supabase.table("products").upsert(formatted_data, on_conflict="Guid").execute()
        logging.info(f"Successfully synced {len(formatted_data)} products.")
    except Exception as e:
        logging.error(f"Error syncing products: {e}")

def sync_customers(customers, company_id, company_name):
    if not supabase or not customers: return
    try:
        formatted_data = []
        for c in customers:
            if not c: continue
            formatted_data.append({
                "customer_name": c.get("NAME"),
                "Guid": c.get("GUID"),
                "mobile_number": c.get("LEDGERPHONE"),
                "address": c.get("ADDRESS"),
                "pincode": c.get("PINCODE"),
                "state": c.get("STATENAME"),
                "gst_number": c.get("PARTYGSTIN"),
                "customer_category_name": c.get("PARENT"),
                "company_id": company_id,
                "company_name": company_name
            })
        
        res = supabase.table("customers").upsert(formatted_data, on_conflict="Guid").execute()
        logging.info(f"Successfully synced {len(formatted_data)} customers.")
    except Exception as e:
        logging.error(f"Error syncing customers: {e}")

def sync_categories(categories, company_id, company_name):
    if not supabase or not categories: return
    try:
        formatted_data = []
        for cat in categories:
            if not cat: continue
            formatted_data.append({
                "category_name": cat.get("NAME"),
                "Guid": cat.get("GUID"),
                "parent": cat.get("PARENT"),
                "company_id": company_id,
                "company_name": company_name
            })
        
        res = supabase.table("customer_categories").upsert(formatted_data, on_conflict="Guid").execute()
        logging.info(f"Successfully synced {len(formatted_data)} customer categories.")
    except Exception as e:
        logging.error(f"Error syncing categories: {e}")

def sync_item_parents(item_parents, company_id, company_name):
    if not supabase or not item_parents: return
    try:
        formatted_data = []
        for parent in item_parents:
            if not parent: continue
            formatted_data.append({
                "ParentName": parent.get("NAME"),
                "Guid": parent.get("GUID"),
                "parent": parent.get("PARENT"),
                "company_id": company_id,
                "company_name": company_name
            })
        
        res = supabase.table("item_parents").upsert(formatted_data, on_conflict="Guid").execute()
        logging.info(f"Successfully synced {len(formatted_data)} item parents.")
    except Exception as e:
        logging.error(f"Error syncing item parents: {e}")
