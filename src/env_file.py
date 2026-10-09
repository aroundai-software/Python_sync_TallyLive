"""Read / write the local .env used by sync + settings UI."""

from __future__ import annotations

import os
import re
from typing import Dict

from .paths import get_env_path

ENV_KEYS = ("TALLY_HOST", "TALLY_PORT", "SYNC_INTERVAL", "FULL_RECONCILE")


def read_env(path: str | None = None) -> Dict[str, str]:
    path = path or get_env_path()
    values = {
        "TALLY_HOST": "localhost",
        "TALLY_PORT": "9000",
        "SYNC_INTERVAL": "1800",
        "FULL_RECONCILE": "0",
    }
    if not os.path.isfile(path):
        return values

    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, raw = line.split("=", 1)
            key = key.strip()
            if key in values:
                values[key] = raw.strip().strip('"').strip("'")
    return values


def write_env(values: Dict[str, str], path: str | None = None) -> str:
    """Write known keys; preserve other lines when file already exists."""
    path = path or get_env_path()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)

    incoming = {k: str(values.get(k, "")).strip() for k in ENV_KEYS}
    # Normalize full reconcile
    fr = incoming.get("FULL_RECONCILE", "0").lower()
    incoming["FULL_RECONCILE"] = "1" if fr in ("1", "true", "yes", "on") else "0"

    existing_lines: list[str] = []
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as f:
            existing_lines = f.read().splitlines()

    written = set()
    out_lines: list[str] = []
    for line in existing_lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in incoming:
                out_lines.append(f"{key}={incoming[key]}")
                written.add(key)
                continue
        out_lines.append(line)

    for key in ENV_KEYS:
        if key not in written:
            out_lines.append(f"{key}={incoming[key]}")

    text = "\n".join(out_lines).rstrip() + "\n"
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    return path


def set_env_key(key: str, value: str, path: str | None = None) -> None:
    values = read_env(path)
    values[key] = value
    write_env(values, path)


def clear_full_reconcile_flag(path: str | None = None) -> bool:
    """Set FULL_RECONCILE=0 after a one-shot full run. Returns True if cleared."""
    values = read_env(path)
    if values.get("FULL_RECONCILE", "0") != "1":
        return False
    values["FULL_RECONCILE"] = "0"
    write_env(values, path)
    return True
