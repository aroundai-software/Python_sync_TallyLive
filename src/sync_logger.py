import logging
import socket
from datetime import datetime, timedelta, timezone

from supabase import create_client

from .config import load_config
from .error_format import truncate_errors
from .version import APP_VERSION

# Define Indian Standard Time (IST) timezone
IST = timezone(timedelta(hours=5, minutes=30))


class SyncLogger:
    """
    Manages a single sync_logs row in Supabase for one full sync cycle.

    Status flow:
        running   → Phase 1 (Tally → local SQLite), step counts update live
        uploading → Phase 3 (local SQLite → Supabase)
        completed / partial / failed → after Phase 3 finishes

    error_details is flushed live on each step failure so you can diagnose
    from Supabase without opening the client machine.
    """

    def __init__(self, total_steps: int = 0):

        config = load_config()

        self._supabase = create_client(
            config["supabase_url"],
            config["supabase_key"]
        )

        self._log_id = None
        self._total_steps = total_steps
        self._steps_passed = 0
        self._steps_failed = 0
        self._error_details = []
        self._started_at = None
        self._company_name = None
        self._upload_failed = False

    def start(self):
        """Insert a 'running' row and store the returned ID."""

        self._started_at = datetime.now(IST)

        payload = {
            "started_at": self._started_at.isoformat(),
            "status": "running",
            "total_steps": self._total_steps,
            "steps_passed": 0,
            "steps_failed": 0,
            "machine_name": socket.gethostname(),
            "error_details": None,
        }
        try:
            payload["app_version"] = APP_VERSION
            result = self._supabase.table("sync_logs").insert(payload).execute()
        except Exception:
            payload.pop("app_version", None)
            result = self._supabase.table("sync_logs").insert(payload).execute()

        if result.data:
            self._log_id = result.data[0]["id"]

    def set_company_name(self, company_name: str):
        """Set the active Tally company name for the sync log."""
        self._company_name = company_name
        if self._log_id:
            try:
                self._supabase.table("sync_logs").update({
                    "company_name": self._company_name
                }).eq("id", self._log_id).execute()
            except Exception as e:
                logging.warning(f"Failed to update company name in sync_logs: {e}")

    def step_passed(self):
        """Record one successful step."""

        self._steps_passed += 1
        self._flush_counts()

    def step_failed(self, error: str = ""):
        """Record one failed step and append the error message (pushed live)."""

        self._steps_failed += 1

        if error:
            self._error_details.append(str(error).strip())

        self._flush_counts(include_errors=True)

    def note_error(self, error: str):
        """Append an error without incrementing steps_failed (e.g. Phase 3 detail)."""
        if error:
            self._error_details.append(str(error).strip())
            self._flush_counts(include_errors=True)

    def mark_uploading(self):
        """Phase 1 done — switch status to 'uploading' while local DB pushes to Supabase."""

        if not self._log_id:
            return

        try:
            self._supabase.table("sync_logs").update({
                "status": "uploading",
                "steps_passed": self._steps_passed,
                "steps_failed": self._steps_failed,
                "company_name": self._company_name,
                "error_details": truncate_errors(self._error_details),
            }).eq("id", self._log_id).execute()
        except Exception as e:
            logging.warning(f"Failed to set sync_logs status to uploading: {e}")

    def finish(self, upload_failed: bool = False, upload_error: str = ""):
        """
        Mark the log row as 'completed', 'partial', or 'failed' after Phase 3
        and calculate the duration.
        """

        if not self._log_id:
            return

        if upload_failed:
            self._upload_failed = True
            if upload_error:
                self._error_details.append(str(upload_error).strip())

        finished_at = datetime.now(IST)

        duration = round(
            (finished_at - self._started_at).total_seconds(),
            2
        )

        if self._steps_failed == 0 and not self._upload_failed:
            status = "completed"
        elif self._steps_passed == 0:
            status = "failed"
        else:
            status = "partial"

        error_text = truncate_errors(self._error_details)

        payload = {
            "finished_at": finished_at.isoformat(),
            "status": status,
            "total_steps": self._total_steps,
            "steps_passed": self._steps_passed,
            "steps_failed": self._steps_failed,
            "error_details": error_text,
            "duration_seconds": duration,
            "company_name": self._company_name,
            "app_version": APP_VERSION,
        }

        try:
            self._supabase.table("sync_logs").update(payload).eq(
                "id", self._log_id
            ).execute()
        except Exception:
            payload.pop("app_version", None)
            try:
                self._supabase.table("sync_logs").update(payload).eq(
                    "id", self._log_id
                ).execute()
            except Exception as e:
                logging.warning(f"Failed to finalize sync_logs row: {e}")

    def abort(self, error: str = ""):
        """
        Mark the log row as 'failed' immediately
        (used when the sync cannot even start).
        """

        if not self._log_id:
            return

        if error:
            self._error_details.append(str(error).strip())

        finished_at = datetime.now(IST)

        duration = round(
            (finished_at - self._started_at).total_seconds(),
            2
        )

        payload = {
            "finished_at": finished_at.isoformat(),
            "status": "failed",
            "steps_passed": self._steps_passed,
            "steps_failed": self._steps_failed,
            "error_details": truncate_errors(self._error_details) or error,
            "duration_seconds": duration,
            "company_name": self._company_name,
            "app_version": APP_VERSION,
        }
        try:
            self._supabase.table("sync_logs").update(payload).eq(
                "id", self._log_id
            ).execute()
        except Exception:
            payload.pop("app_version", None)
            try:
                self._supabase.table("sync_logs").update(payload).eq(
                    "id", self._log_id
                ).execute()
            except Exception as e:
                logging.warning(f"Failed to abort sync_logs row: {e}")

    def _flush_counts(self, include_errors: bool = False):
        """Push live step counters (and optionally errors) to Supabase mid-sync."""

        if not self._log_id:
            return

        payload = {
            "steps_passed": self._steps_passed,
            "steps_failed": self._steps_failed,
        }
        if include_errors or self._error_details:
            payload["error_details"] = truncate_errors(self._error_details)

        try:
            self._supabase.table("sync_logs").update(payload).eq(
                "id", self._log_id
            ).execute()
        except Exception as e:
            logging.warning(f"Failed to flush sync_logs counts: {e}")
