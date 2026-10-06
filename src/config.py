import logging
import os

def load_config():
    from dotenv import load_dotenv
    load_dotenv(override=True)
    
    tally_host = os.environ.get("TALLY_HOST", "localhost").strip()
    if not tally_host.startswith("http://") and not tally_host.startswith("https://"):
        tally_host = f"http://{tally_host}"
    
    tally_port = str(os.environ.get("TALLY_PORT", 9000)).strip()
    tally_url = f"{tally_host}:{tally_port}" if tally_port else tally_host

    supabase_url = (os.environ.get("SUPABASE_URL") or "").strip()
    supabase_key = (os.environ.get("SUPABASE_KEY") or "").strip()
    if not supabase_url or not supabase_key:
        raise ValueError(
            "SUPABASE_URL and SUPABASE_KEY must be set in the .env file. "
            "See .env.example for the expected settings."
        )

    return {
        "tally_host": os.environ.get("TALLY_HOST", "localhost"),
        "tally_port": tally_port,
        "tally_url": tally_url,
        "sync_interval": int(os.environ.get("SYNC_INTERVAL", 1800)),
        "supabase_url": supabase_url,
        "supabase_key": supabase_key
    }


if __name__ == "__main__":
    config = load_config()

   