"""Resolve install / runtime directories for frozen EXE and dev runs."""

from __future__ import annotations

import os
import shutil
import sys


def get_app_dir() -> str:
    """Directory containing the sync EXE (or project root in dev)."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def get_env_path() -> str:
    """
    .env next to the sync EXE (Program Files install folder).

    Same path Settings and the sync service use.
    """
    return os.path.join(get_app_dir(), ".env")


def legacy_env_path() -> str:
    """Alias kept for older imports — same as get_env_path()."""
    return get_env_path()


def get_config_dir() -> str:
    """Install/app directory (where .env lives)."""
    return get_app_dir()


def ensure_writable_env_path() -> str:
    """
    Path used when Settings saves.

    Uses Program Files (install) .env. If a ProgramData copy exists from
    older 1.2.0 builds and install .env is missing, copy it back once.
    Saving may require running Settings as Administrator.
    """
    path = get_env_path()
    program_data = os.environ.get("PROGRAMDATA") or ""
    pd_env = (
        os.path.join(program_data, "TallyLive Sync", ".env") if program_data else ""
    )

    if not os.path.isfile(path) and pd_env and os.path.isfile(pd_env):
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            shutil.copy2(pd_env, path)
        except OSError:
            pass

    return path


def get_sync_log_path() -> str:
    """Log file next to the sync EXE."""
    return os.path.join(get_app_dir(), "sync.log")


def get_install_root() -> str:
    """
    Parent of the sync folder when installed as:
      <root>\\Tallylive_python_sync_background\\EXE
    Falls back to app dir in dev / flat layouts.
    """
    app_dir = get_app_dir()
    if os.path.basename(app_dir).lower() == "tallylive_python_sync_background":
        return os.path.dirname(app_dir)
    return app_dir


def promote_settings_from_payload() -> bool:
    """
    After auto-update, Settings may land in sync/_update_payload/ so older
    updaters can extract a single-top-level zip. Copy it to the install root
    beside the sync folder, then remove the payload directory.
    Returns True if a promote was attempted and succeeded.
    """
    settings_name = "Tallylive_sync_settings.exe"
    payload = os.path.join(get_app_dir(), "_update_payload", settings_name)
    if not os.path.isfile(payload):
        return False
    dest = os.path.join(get_install_root(), settings_name)
    tmp = dest + ".new"
    try:
        shutil.copy2(payload, tmp)
        if os.path.isfile(dest):
            aside = dest + ".old"
            try:
                if os.path.isfile(aside):
                    os.remove(aside)
                os.replace(dest, aside)
            except OSError:
                pass
        os.replace(tmp, dest)
        shutil.rmtree(
            os.path.join(get_app_dir(), "_update_payload"), ignore_errors=True
        )
        return True
    except OSError:
        try:
            if os.path.isfile(tmp):
                os.remove(tmp)
        except OSError:
            pass
        return False


def get_updater_path() -> str:
    root = get_install_root()
    candidates = [
        os.path.join(root, "Tallylive_updater.exe"),
        os.path.join(get_app_dir(), "Tallylive_updater.exe"),
    ]
    for path in candidates:
        if os.path.isfile(path):
            return path
    return candidates[0]
