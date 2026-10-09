"""Format exceptions for sync_logs.error_details (readable in Supabase)."""

from __future__ import annotations

import traceback
from typing import Any


def format_error(exc: BaseException, *, max_len: int = 1500) -> str:
    """
    Build a concise but specific error string for remote diagnosis.
    Prefers API/PostgREST fields (message, code, details, hint) when present.
    """
    parts: list[str] = [f"{type(exc).__name__}: {exc}"]

    # supabase / postgrest / httpx style payloads
    for attr in ("message", "code", "details", "hint", "status_code"):
        val = getattr(exc, attr, None)
        if val not in (None, "", parts[0]):
            parts.append(f"{attr}={val}")

    args = getattr(exc, "args", None)
    if args and len(args) == 1 and isinstance(args[0], dict):
        d: dict[str, Any] = args[0]
        for key in ("message", "code", "details", "hint"):
            if d.get(key) not in (None, ""):
                parts.append(f"{key}={d.get(key)}")

    # One-line location (file:line) — skip huge stacks
    tb = traceback.extract_tb(exc.__traceback__) if exc.__traceback__ else []
    if tb:
        last = tb[-1]
        parts.append(f"at {last.filename}:{last.lineno} in {last.name}")

    text = " | ".join(str(p) for p in parts if p)
    if len(text) > max_len:
        return text[: max_len - 3] + "..."
    return text


def truncate_errors(lines: list[str], *, max_total: int = 8000) -> str | None:
    if not lines:
        return None
    text = "\n".join(lines)
    if len(text) <= max_total:
        return text
    return text[: max_total - 20] + "\n...[truncated]..."
