"""Shared Tally HTTP timeout helpers (local vs remote/cloud)."""

from __future__ import annotations

import os
from typing import Optional, Union

from .config import load_config


def is_remote_tally(tally_host: Optional[str] = None) -> bool:
    """True when Tally is not on this machine (cloud / remote host)."""
    host = (tally_host or "").strip()
    if not host:
        try:
            host = str(load_config().get("tally_host") or "")
        except Exception:
            host = ""
    host = (
        host.lower()
        .replace("http://", "")
        .replace("https://", "")
        .split(":")[0]
        .strip()
    )
    return host not in ("", "localhost", "127.0.0.1", "::1")


def tally_http_timeout(
    local_read: int = 180,
    remote_read: Optional[int] = None,
    connect: int = 20,
) -> Union[int, tuple]:
    """
    requests timeout: int seconds (local) or (connect, read) for remote Tally.
    Override remote read with TALLY_REMOTE_READ_TIMEOUT (seconds).
    """
    if not is_remote_tally():
        return max(30, int(local_read))

    if remote_read is None:
        try:
            remote_read = int(
                os.environ.get("TALLY_REMOTE_READ_TIMEOUT", "300").strip()
            )
        except ValueError:
            remote_read = 300
    remote_read = max(60, min(int(remote_read), 900))
    return (max(5, int(connect)), remote_read)
