"""
TallyLive Sync Settings — edit host/port/interval/full reconcile; show app version.
"""

from __future__ import annotations

import os
import sys
import tkinter as tk
from tkinter import messagebox, ttk

# Allow running from source tree
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from src.env_file import read_env, write_env  # noqa: E402
from src.version import APP_NAME, APP_VERSION  # noqa: E402


def resolve_env_path() -> str:
    """Find .env next to settings EXE or under Tallylive_python_sync_background."""
    if getattr(sys, "frozen", False):
        base = os.path.dirname(sys.executable)
    else:
        base = _ROOT

    candidates = [
        os.path.join(base, ".env"),
        os.path.join(base, "Tallylive_python_sync_background", ".env"),
        os.path.join(os.path.dirname(base), "Tallylive_python_sync_background", ".env"),
    ]
    for path in candidates:
        if os.path.isfile(path):
            return path
    # Default write location when installed beside sync folder
    preferred = os.path.join(base, "Tallylive_python_sync_background", ".env")
    if os.path.isdir(os.path.dirname(preferred)):
        return preferred
    return os.path.join(base, ".env")


class SettingsApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"{APP_NAME} Settings")
        self.resizable(False, False)
        self.env_path = resolve_env_path()
        self.values = read_env(self.env_path)

        pad = {"padx": 12, "pady": 6}
        frm = ttk.Frame(self, padding=16)
        frm.grid(row=0, column=0, sticky="nsew")

        ttk.Label(frm, text=f"{APP_NAME}", font=("Segoe UI", 14, "bold")).grid(
            row=0, column=0, columnspan=2, sticky="w", **pad
        )
        ttk.Label(frm, text=f"Version {APP_VERSION}", font=("Segoe UI", 10)).grid(
            row=1, column=0, columnspan=2, sticky="w", padx=12, pady=(0, 10)
        )

        self.host_var = tk.StringVar(value=self.values.get("TALLY_HOST", "localhost"))
        self.port_var = tk.StringVar(value=self.values.get("TALLY_PORT", "9000"))
        interval_sec = int(self.values.get("SYNC_INTERVAL", "1800") or 1800)
        self.interval_min_var = tk.StringVar(value=str(max(1, interval_sec // 60)))
        self.full_var = tk.BooleanVar(
            value=self.values.get("FULL_RECONCILE", "0") in ("1", "true", "yes")
        )

        ttk.Label(frm, text="Tally host").grid(row=2, column=0, sticky="w", **pad)
        ttk.Entry(frm, textvariable=self.host_var, width=36).grid(
            row=2, column=1, sticky="we", **pad
        )

        ttk.Label(frm, text="Port").grid(row=3, column=0, sticky="w", **pad)
        ttk.Entry(frm, textvariable=self.port_var, width=12).grid(
            row=3, column=1, sticky="w", **pad
        )

        ttk.Label(frm, text="Sync interval (minutes)").grid(
            row=4, column=0, sticky="w", **pad
        )
        ttk.Entry(frm, textvariable=self.interval_min_var, width=12).grid(
            row=4, column=1, sticky="w", **pad
        )

        ttk.Checkbutton(
            frm,
            text="Full reconcile on next sync (one-time, then clears)",
            variable=self.full_var,
        ).grid(row=5, column=0, columnspan=2, sticky="w", **pad)

        btn_row = ttk.Frame(frm)
        btn_row.grid(row=6, column=0, columnspan=2, sticky="e", pady=(12, 0))
        ttk.Button(btn_row, text="Save", command=self.save).pack(side="right", padx=4)
        ttk.Button(btn_row, text="Close", command=self.destroy).pack(side="right", padx=4)

    def save(self):
        host = self.host_var.get().strip() or "localhost"
        port = self.port_var.get().strip()
        try:
            port_n = int(port)
            if not (1 <= port_n <= 65535):
                raise ValueError("port range")
        except ValueError:
            messagebox.showerror("Invalid port", "Port must be a number between 1 and 65535.")
            return

        try:
            minutes = int(self.interval_min_var.get().strip())
            if minutes < 1:
                raise ValueError("minutes")
        except ValueError:
            messagebox.showerror("Invalid interval", "Sync interval must be at least 1 minute.")
            return

        try:
            write_env(
                {
                    "TALLY_HOST": host,
                    "TALLY_PORT": str(port_n),
                    "SYNC_INTERVAL": str(minutes * 60),
                    "FULL_RECONCILE": "1" if self.full_var.get() else "0",
                },
                self.env_path,
            )
        except Exception as e:
            messagebox.showerror(
                "Save failed",
                f"Could not write settings.\n\n{e}\n\n"
                "Try running Settings as Administrator if the install folder is protected.",
            )
            return

        messagebox.showinfo(
            "Saved",
            "Settings saved.\nChanges apply on the next sync cycle.",
        )


def main():
    app = SettingsApp()
    app.mainloop()


if __name__ == "__main__":
    main()
