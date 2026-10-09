"""Resolve install / runtime directories for frozen EXE and dev runs."""

from __future__ import annotations

import os
import sys


def get_app_dir() -> str:
    """Directory containing the sync EXE (or project root in dev)."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def get_env_path() -> str:
    return os.path.join(get_app_dir(), ".env")


def get_sync_log_path() -> str:
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
