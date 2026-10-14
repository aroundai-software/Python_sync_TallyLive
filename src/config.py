import base64
import json
import os

# Baked in so a packaged EXE runs on a client machine with no .env present.
# Do not put these in .env (and never put the service key in the Flutter app).
SUPABASE_URL = "https://gwerbqebixlmniqzlhuk.supabase.co"
SUPABASE_ANON_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Imd3ZXJicWViaXhsbW5pcXpsaHVrIiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODU5MTAwOTUsImV4cCI6MjEwMTQ4NjA5NX0.qq7Ddl-SxH6glpUypFDrD8Jf5sQ8uPd85QhfTGtqvOE"
SUPABASE_SERVICE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Imd3ZXJicWViaXhsbW5pcXpsaHVrIiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc4NTkxMDA5NSwiZXhwIjoyMTAxNDg2MDk1fQ.bUTrSpgvqep19fxo-aki0187k51O59m8xnlUAw6rl2c"
# Legacy alias used by sync client creation paths.
SUPABASE_KEY = SUPABASE_SERVICE_KEY


def _jwt_role(key: str) -> str:
    """Return the JWT role claim (anon / service_role) or empty string."""
    if not key or key.count(".") < 2:
        return ""
    try:
        payload = key.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload.encode("utf-8")))
        return str(data.get("role") or "")
    except Exception:
        return ""


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

    # Credentials stay in this file only — not in .env.
    supabase_key = SUPABASE_SERVICE_KEY
    using_service_role = _jwt_role(supabase_key) == "service_role"

    return {
        "tally_host": os.environ.get("TALLY_HOST", "localhost"),
        "tally_port": tally_port,
        "tally_url": tally_url,
        "sync_interval": sync_interval,
        "supabase_url": SUPABASE_URL,
        "supabase_key": supabase_key,
        "supabase_anon_key": SUPABASE_ANON_KEY,
        "supabase_using_service_role": using_service_role,
    }


if __name__ == "__main__":
    config = load_config()
    print("using_service_role=", config["supabase_using_service_role"])
