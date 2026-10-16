"""Resolve install / runtime directories for frozen EXE and dev runs."""

from __future__ import annotations

import os
import shutil
import sys

SYNC_FOLDER_NAME = "Tallylive_python_sync_background"


def get_app_dir() -> str:
    """Directory containing this frozen EXE (or project root in dev)."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def get_install_root() -> str:
    """
    Install root:
      <root>\\Tallylive_python_sync_background\\EXE   → <root>
      <root>\\Tallylive_sync_settings.exe             → <root>
    Falls back to app dir in dev / flat layouts.
    """
    app_dir = get_app_dir()
    if os.path.basename(app_dir).lower() == SYNC_FOLDER_NAME.lower():
        return os.path.dirname(app_dir)
    return app_dir


def get_sync_dir() -> str:
    """
    Folder that contains the sync background EXE (where .env / sync.log live).

    Settings EXE lives in the install root; sync EXE lives one level down.
    Always resolve to the sync folder when present so both share one .env.
    """
    app_dir = get_app_dir()
    if os.path.basename(app_dir).lower() == SYNC_FOLDER_NAME.lower():
        return app_dir
    nested = os.path.join(app_dir, SYNC_FOLDER_NAME)
    if os.path.isdir(nested):
        return nested
    return app_dir


def get_env_path() -> str:
    """
    .env next to the sync EXE:
      Program Files\\TallyLive Sync\\Tallylive_python_sync_background\\.env

    Settings and the sync service both use this path.
    """
    return os.path.join(get_sync_dir(), ".env")


def legacy_env_path() -> str:
    """Alias kept for older imports — same as get_env_path()."""
    return get_env_path()


def get_config_dir() -> str:
    """Directory where .env lives (sync folder)."""
    return get_sync_dir()


def ensure_writable_env_path() -> str:
    """
    Path used when Settings saves (sync-folder .env).

    Migrates a one-time copy from older mistaken locations if needed:
      - install-root .env (Settings used to write here)
      - ProgramData\\TallyLive Sync\\.env (1.2.0 detour)
    Saving may require running Settings as Administrator.
    """
    path = get_env_path()
    install_root = get_install_root()
    root_env = os.path.join(install_root, ".env")
    program_data = os.environ.get("PROGRAMDATA") or ""
    pd_env = (
        os.path.join(program_data, "TallyLive Sync", ".env") if program_data else ""
    )

    # Prefer copying a newer/misplaced Settings save into the sync folder.
    # If sync .env is missing, take root then ProgramData.
    # If both exist and root was written more recently (Settings save), prefer root.
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        if os.path.isfile(root_env):
            if (not os.path.isfile(path)) or (
                os.path.getmtime(root_env) > os.path.getmtime(path)
            ):
                shutil.copy2(root_env, path)
        elif not os.path.isfile(path) and pd_env and os.path.isfile(pd_env):
            shutil.copy2(pd_env, path)
    except OSError:
        pass

    return path


def get_sync_log_path() -> str:
    """Log file next to the sync EXE."""
    return os.path.join(get_sync_dir(), "sync.log")


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
