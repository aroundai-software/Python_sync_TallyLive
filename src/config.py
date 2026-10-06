import logging
import os

SUPABASE_URL = "https://gwerbqebixlmniqzlhuk.supabase.co"
SUPABASE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Imd3ZXJicWViaXhsbW5pcXpsaHVrIiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODU5MTAwOTUsImV4cCI6MjEwMTQ4NjA5NX0.qq7Ddl-SxH6glpUypFDrD8Jf5sQ8uPd85QhfTGtqvOE"

def load_config():
    from dotenv import load_dotenv
    load_dotenv(override=True)
    
    tally_host = os.environ.get("TALLY_HOST", "localhost").strip()
    if not tally_host.startswith("http://") and not tally_host.startswith("https://"):
        tally_host = f"http://{tally_host}"
    
    tally_port = str(os.environ.get("TALLY_PORT", 9000)).strip()
    tally_url = f"{tally_host}:{tally_port}" if tally_port else tally_host
    
    return {
        "tally_host": os.environ.get("TALLY_HOST", "localhost"),
        "tally_port": tally_port,
        "tally_url": tally_url,
        "sync_interval": int(os.environ.get("SYNC_INTERVAL", 1800)),
        "supabase_url": os.environ.get("SUPABASE_URL") or SUPABASE_URL,
        "supabase_key": os.environ.get("SUPABASE_KEY") or SUPABASE_KEY
    }


if __name__ == "__main__":
    config = load_config()

   