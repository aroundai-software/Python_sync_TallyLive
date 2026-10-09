"""
Check Supabase for a newer sync build, download + verify, hand off to updater.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
import tempfile
import zipfile
from datetime import datetime, timezone

from .config import load_config
from .paths import get_app_dir, get_install_root, get_updater_path
from .version import APP_VERSION

BUCKET = "sync-releases"
PENDING_NAME = "update_pending.json"


def _parse_version(v: str) -> tuple:
    parts = [int(x) for x in re.findall(r"\d+", (v or "").strip())] or [0]
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:3])


def is_newer(remote: str, local: str) -> bool:
    return _parse_version(remote) > _parse_version(local)


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def check_and_apply_update() -> bool:
    """
    Returns True if an update was staged and the process should exit
    so the updater can replace files.
    """
    try:
        config = load_config()
        from supabase import create_client

        supabase = create_client(config["supabase_url"], config["supabase_key"])
        result = (
            supabase.table("sync_app_releases")
            .select("*")
            .eq("is_active", True)
            .order("released_at", desc=True)
            .limit(1)
            .execute()
        )
        rows = result.data or []
        if not rows:
            logging.info("[Update] No active release in sync_app_releases.")
            return False

        release = rows[0]
        remote_ver = str(release.get("version") or "").strip()
        storage_path = str(release.get("storage_path") or "").strip()
        expected_hash = str(release.get("sha256") or "").strip().lower()

        if not remote_ver or not storage_path:
            logging.warning("[Update] Active release missing version/storage_path.")
            return False

        if not is_newer(remote_ver, APP_VERSION):
            logging.info(
                f"[Update] Up to date (local={APP_VERSION}, remote={remote_ver})."
            )
            return False

        logging.info(
            f"[Update] New version available: {remote_ver} (local={APP_VERSION}). Downloading..."
        )

        raw = supabase.storage.from_(BUCKET).download(storage_path)
        if not raw:
            logging.error("[Update] Empty download from Storage.")
            return False

        tmp_dir = tempfile.mkdtemp(prefix="tallylive_update_")
        zip_path = os.path.join(tmp_dir, "update.zip")
        with open(zip_path, "wb") as f:
            f.write(raw)

        actual_hash = _sha256_file(zip_path)
        if expected_hash and actual_hash != expected_hash:
            logging.error(
                f"[Update] SHA256 mismatch (expected={expected_hash}, got={actual_hash}). Aborting."
            )
            return False

        if not zipfile.is_zipfile(zip_path):
            logging.error("[Update] Downloaded file is not a valid zip. Aborting.")
            return False

        pending = {
            "version": remote_ver,
            "zip_path": zip_path,
            "target_dir": get_app_dir(),
            "install_root": get_install_root(),
            "sha256": actual_hash,
            "requested_at": datetime.now(timezone.utc).isoformat(),
            "from_version": APP_VERSION,
        }
        pending_path = os.path.join(get_install_root(), PENDING_NAME)
        with open(pending_path, "w", encoding="utf-8") as f:
            json.dump(pending, f, indent=2)

        updater = get_updater_path()
        if not os.path.isfile(updater):
            logging.error(
                f"[Update] Updater not found at {updater}. Staged pending file only."
            )
            return False

        logging.info(f"[Update] Launching updater: {updater}")
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        subprocess.Popen(
            [updater, pending_path],
            cwd=os.path.dirname(updater),
            close_fds=True,
            creationflags=flags,
        )
        return True

    except Exception as e:
        msg = str(e)
        if "sync_app_releases" in msg or "42P01" in msg or "404" in msg:
            logging.info(f"[Update] Release channel not ready yet ({e}). Skipping.")
        else:
            logging.warning(f"[Update] Check failed (sync continues): {e}")
        return False
