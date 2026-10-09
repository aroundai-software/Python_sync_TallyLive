import os

# Baked in so a packaged EXE runs on a client machine with no .env present.
# An environment variable still wins if one is set.
SUPABASE_URL = "https://gwerbqebixlmniqzlhuk.supabase.co"
SUPABASE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Imd3ZXJicWViaXhsbW5pcXpsaHVrIiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODU5MTAwOTUsImV4cCI6MjEwMTQ4NjA5NX0.qq7Ddl-SxH6glpUypFDrD8Jf5sQ8uPd85QhfTGtqvOE"


def load_config():
    from dotenv import load_dotenv

    from .paths import get_env_path

    env_path = get_env_path()
    if os.path.isfile(env_path):
        load_dotenv(env_path, override=True)
    else:
        load_dotenv(override=True)

    tally_host = os.environ.get("TALLY_HOST", "localhost").strip()
    if not tally_host.startswith("http://") and not tally_host.startswith("https://"):
        tally_host = f"http://{tally_host}"

    tally_port = str(os.environ.get("TALLY_PORT", 9000)).strip()
    tally_url = f"{tally_host}:{tally_port}" if tally_port else tally_host

    try:
        sync_interval = int(os.environ.get("SYNC_INTERVAL", 1800))
    except (TypeError, ValueError):
        sync_interval = 1800

    return {
        "tally_host": os.environ.get("TALLY_HOST", "localhost"),
        "tally_port": tally_port,
        "tally_url": tally_url,
        "sync_interval": sync_interval,
        "supabase_url": (os.environ.get("SUPABASE_URL") or "").strip() or SUPABASE_URL,
        "supabase_key": (os.environ.get("SUPABASE_KEY") or "").strip() or SUPABASE_KEY,
    }


if __name__ == "__main__":
    config = load_config()
