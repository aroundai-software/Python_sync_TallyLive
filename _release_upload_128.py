"""Upload 1.2.8 update zip to sync-releases and activate sync_app_releases."""
from __future__ import annotations

import os
from pathlib import Path

from supabase import create_client

from src.config import load_config
from src.version import APP_VERSION

BUCKET = "sync-releases"
ZIP_NAME = f"Tallylive_python_sync_background_{APP_VERSION}.zip"
STORAGE_PATH = f"{APP_VERSION}/{ZIP_NAME}"
NOTES = (
    "Multi-year BS/P&L/cash/ratio for continuous companies; "
    "FY-capped exports with throttle to avoid freezing Tally."
)


def main() -> None:
    base = Path(__file__).resolve().parent
    zip_path = base / "installer_output" / ZIP_NAME
    sha_path = Path(str(zip_path) + ".sha256.txt")
    if not zip_path.is_file():
        raise SystemExit(f"Missing zip: {zip_path}")
    digest = sha_path.read_text(encoding="utf-8").splitlines()[0].strip()
    size_mb = zip_path.stat().st_size / (1024 * 1024)
    print(f"version={APP_VERSION}")
    print(f"zip={zip_path} ({size_mb:.1f} MB)")
    print(f"sha256={digest}")
    print(f"storage_path={STORAGE_PATH}")

    cfg = load_config()
    sb = create_client(cfg["supabase_url"], cfg["supabase_key"])

    data = zip_path.read_bytes()
    print(f"Uploading to {BUCKET}/{STORAGE_PATH} ...")
    # upsert overwrite if re-releasing same version
    sb.storage.from_(BUCKET).upload(
        STORAGE_PATH,
        data,
        file_options={
            "content-type": "application/zip",
            "upsert": "true",
        },
    )
    print("Upload OK")

    # Deactivate other versions, upsert/activate this one
    sb.table("sync_app_releases").update({"is_active": False}).neq(
        "version", APP_VERSION
    ).execute()

    row = {
        "version": APP_VERSION,
        "storage_path": STORAGE_PATH,
        "sha256": digest,
        "is_active": True,
        "mandatory": True,
        "notes": NOTES,
    }
    existing = (
        sb.table("sync_app_releases")
        .select("id,version")
        .eq("version", APP_VERSION)
        .limit(1)
        .execute()
        .data
        or []
    )
    if existing:
        sb.table("sync_app_releases").update(row).eq("version", APP_VERSION).execute()
        print(f"Updated existing sync_app_releases row for {APP_VERSION}")
    else:
        sb.table("sync_app_releases").insert(row).execute()
        print(f"Inserted sync_app_releases row for {APP_VERSION}")

    active = (
        sb.table("sync_app_releases")
        .select("version,storage_path,sha256,is_active,mandatory,notes,released_at")
        .eq("is_active", True)
        .execute()
        .data
        or []
    )
    print("ACTIVE RELEASES:")
    for r in active:
        print(r)


if __name__ == "__main__":
    main()
