import logging
from datetime import datetime, timezone, timedelta
import socket

from supabase import create_client

from .config import load_config

# Define Indian Standard Time (IST) timezone
IST = timezone(timedelta(hours=5, minutes=30))

class SyncLogger:
    """
    Manages a single sync_logs row in Supabase for one full sync cycle.

    Usage:
        logger = SyncLogger(total_steps=5)
        logger.start()

        try:
            do_step_1()
            logger.step_passed()
        except Exception as e:
            logger.step_failed(str(e))

        logger.finish()
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

    # --------------------------------------------------
    # Public API
    # --------------------------------------------------

    def start(self):
        """Insert a 'running' row and store the returned ID."""

        self._started_at = datetime.now(IST)

        result = (
            self._supabase
            .table("sync_logs")
            .insert({
                "started_at":
                    self._started_at.isoformat(),

                "status":
                    "running",

                "total_steps":
                    self._total_steps,

                "steps_passed":
                    0,

                "steps_failed":
                    0,
                "machine_name":
                    socket.gethostname(),
            })
            .execute()
        )

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
        """Record one failed step and append the error message."""

        self._steps_failed += 1

        if error:
            self._error_details.append(error)

        self._flush_counts()

    def finish(self):
        """
        Mark the log row as 'completed' or 'partial' and
        calculate the duration.
        """

        if not self._log_id:
            return

        finished_at = datetime.now(IST)

        duration = round(
            (finished_at - self._started_at).total_seconds(),
            2
        )

        if self._steps_failed == 0:
            status = "completed"
        elif self._steps_passed == 0:
            status = "failed"
        else:
            status = "partial"

        error_text = (
            "\n".join(self._error_details)
            if self._error_details
            else None
        )

        self._supabase.table("sync_logs").update({
            "finished_at":
                finished_at.isoformat(),

            "status":
                status,

            "total_steps":
                self._total_steps,

            "steps_passed":
                self._steps_passed,

            "steps_failed":
                self._steps_failed,

            "error_details":
                error_text,

            "duration_seconds":
                duration,

            "company_name":
                self._company_name,
        }).eq("id", self._log_id).execute()

    def abort(self, error: str = ""):
        """
        Mark the log row as 'failed' immediately
        (used when the sync cannot even start).
        """

        if not self._log_id:
            return

        finished_at = datetime.now(IST)

        duration = round(
            (finished_at - self._started_at).total_seconds(),
            2
        )

        self._supabase.table("sync_logs").update({
            "finished_at":
                finished_at.isoformat(),

            "status":
                "failed",

            "steps_passed":
                self._steps_passed,

            "steps_failed":
                self._steps_failed,

            "error_details":
                error,

            "duration_seconds":
                duration,

            "company_name":
                self._company_name,
        }).eq("id", self._log_id).execute()

    # --------------------------------------------------
    # Internal
    # --------------------------------------------------

    def _flush_counts(self):
        """Push live step counters to Supabase mid-sync."""

        if not self._log_id:
            return

        self._supabase.table("sync_logs").update({
            "steps_passed": self._steps_passed,
            "steps_failed": self._steps_failed,
        }).eq("id", self._log_id).execute()
