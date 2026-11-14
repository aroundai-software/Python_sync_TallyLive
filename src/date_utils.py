import os
import logging
import re
from datetime import datetime, timedelta
from dateutil.relativedelta import relativedelta
from dotenv import load_dotenv

# Named year companies (e.g. "GlobeMate (2024-2025)", "Foo 2026-27") keep that FY.
_YEAR_COMPANY_NAME_RE = re.compile(
    r"(?:"
    r"20\d{2}\s*[-–/]\s*20\d{2}"  # 2024-2025 / 2024/2025
    r"|20\d{2}\s*[-–/]\s*\d{2}\b"  # 2026-27
    r"|\(\s*20\d{2}\s*[-–/]\s*20\d{2}\s*\)"
    r"|\(\s*20\d{2}\s*[-–/]\s*\d{2}\s*\)"
    r")",
    re.IGNORECASE,
)

# Hard cap so BS/P&L/cash/ratio never ask Tally for a multi-year dump (freeze risk).
_MAX_REPORT_SPAN_DAYS = 400


def looks_like_year_company_name(company_name: str | None) -> bool:
    """True when the company name encodes a specific FY (year-isolation files)."""
    return bool(_YEAR_COMPANY_NAME_RE.search(company_name or ""))


def current_indian_fy_start(as_on=None):
    """1 April of the Indian FY that contains ``as_on``."""
    from datetime import date as date_cls

    if as_on is None:
        as_on = date_cls.today()
    elif isinstance(as_on, datetime):
        as_on = as_on.date()
    if as_on.month >= 4:
        return date_cls(as_on.year, 4, 1)
    return date_cls(as_on.year - 1, 4, 1)

def get_monthly_date_ranges(books_from_iso=None):
    """
    Returns a list of tuples (start_date, end_date) formatted as YYYYMMDD
    for each month from books_from_iso up to the current month.
    If books_from_iso is missing or invalid, defaults to beginning of current year.
    """
    try:
        if books_from_iso:
            start_date = datetime.fromisoformat(books_from_iso).replace(day=1)
        else:
            start_date = datetime.now().replace(month=1, day=1)
    except Exception:
        start_date = datetime.now().replace(month=1, day=1)

    end_date = datetime.now()
    ranges = []

    current = start_date
    while current <= end_date:
        # End of the current month
        next_month = current + relativedelta(months=1)
        month_end = next_month - relativedelta(days=1)
        
        # Format as YYYYMMDD
        ranges.append((
            current.strftime("%Y%m%d"),
            month_end.strftime("%Y%m%d")
        ))
        
        current = next_month

    return ranges

def get_current_fy_monthly_date_ranges():
    """
    Returns a list of tuples (start_date, end_date) formatted as YYYYMMDD
    for each month.
    If SYNC_START_YEAR is set in .env (e.g. 2025), generates chunks for that
    entire financial year (2025-04-01 to 2026-03-31).
    Otherwise, defaults dynamically to the start of the current Indian Financial Year up to current month.
    """
    load_dotenv(override=True)
    custom_year = os.environ.get("SYNC_START_YEAR", "").strip()

    if custom_year and custom_year.isdigit():
        yr = int(custom_year)
        start_date = datetime(yr, 4, 1)
        end_date = datetime(yr + 1, 3, 31)
    else:
        now = datetime.now()
        if now.month >= 4:
            start_date = datetime(now.year, 4, 1)
        else:
            start_date = datetime(now.year - 1, 4, 1)
        end_date = datetime.now()

    ranges = []
    current = start_date
    while current <= end_date:
        next_month = current + relativedelta(months=1)
        month_end = next_month - relativedelta(days=1)
        
        ranges.append((
            current.strftime("%Y%m%d"),
            month_end.strftime("%Y%m%d")
        ))
        current = next_month

    return ranges


def tally_ymd_to_iso(ymd):
    """Convert Tally YYYYMMDD (or ISO) to YYYY-MM-DD. Empty if unusable."""
    text = str(ymd or "").strip()
    if len(text) >= 10 and text[4] == "-" and text[7] == "-":
        return text[:10]
    digits = "".join(ch for ch in text if ch.isdigit())
    if len(digits) >= 8:
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"
    return ""


def iso_window_from_tally_ranges(date_ranges):
    """
    Inclusive ISO date window covering the first chunk start through the last chunk end.
    Used so reconcile only deletes vouchers inside the same window Tally was asked for.
    """
    if not date_ranges:
        return "", ""
    start = tally_ymd_to_iso(date_ranges[0][0])
    end = tally_ymd_to_iso(date_ranges[-1][1])
    return start, end


def company_period_start(as_on=None, books_from_iso=None):
    """
    Period start for this Tally company: its books_from when known.
    SYNC_START_YEAR still pins a single Indian FY (explicit override).
    Otherwise fall back to the current Indian FY (1 April).
    """
    from datetime import date as date_cls

    if as_on is None:
        as_on = date_cls.today()
    elif isinstance(as_on, datetime):
        as_on = as_on.date()

    load_dotenv(override=True)
    custom_year = os.environ.get("SYNC_START_YEAR", "").strip()
    if custom_year and custom_year.isdigit():
        return date_cls(int(custom_year), 4, 1)

    iso = tally_ymd_to_iso(books_from_iso) if books_from_iso else ""
    if iso:
        try:
            return date_cls.fromisoformat(iso)
        except Exception:
            pass

    if as_on.month >= 4:
        return date_cls(as_on.year, 4, 1)
    return date_cls(as_on.year - 1, 4, 1)


def indian_fy_end_for(start):
    """
    Last day of the Indian financial year that contains ``start``.
    Apr–Mar FY: 2024-04-01 → 2025-03-31; 2025-01-15 → 2025-03-31.
    """
    from datetime import date as date_cls

    if isinstance(start, datetime):
        start = start.date()
    if start.month >= 4:
        return date_cls(start.year + 1, 3, 31)
    return date_cls(start.year, 3, 31)


def company_period_bounds(as_on=None, books_from_iso=None, company_name=None):
    """
    Report window for BS / P&L / cash / ratio — always at most one Indian FY
    so a single Tally export cannot span a decade and freeze the client.

    Rules:
      - books_from inside current FY → books_from … as_on
      - Named year company (name has 2024-25 etc.) whose FY already ended
        → that FY only (books_from … FY end), even if as_on is later
      - Continuous company (old books_from, no year in name) → current FY … as_on
        (fixes empty 2016-17 snapshots while vouchers still cover all years)

    Examples (as_on = 2026-11-09):
      books_from 2026-04-01 → 2026-04-01 … 2026-11-09
      books_from 2024-04-01, name "X (2024-2025)" → 2024-04-01 … 2025-03-31
      books_from 2016-04-01, name "SAMAGRA …" → 2026-04-01 … 2026-11-09
    """
    from datetime import date as date_cls

    if as_on is None:
        as_on = date_cls.today()
    elif isinstance(as_on, datetime):
        as_on = as_on.date()

    books_start = company_period_start(as_on, books_from_iso)
    books_fy_end = indian_fy_end_for(books_start)

    if books_fy_end >= as_on:
        # Live FY for this company file (e.g. 2026-27 year company, or new books).
        start, end = books_start, as_on
    elif looks_like_year_company_name(company_name):
        # Closed / named year file opened later — keep that FY only.
        start, end = books_start, books_fy_end
    else:
        # Continuous multi-year company: current FY only (anti-freeze).
        start = current_indian_fy_start(as_on)
        if books_start > start:
            start = books_start
        end = as_on

    if end < start:
        end = start

    # Absolute safety: never hand Tally a multi-year report window.
    if (end - start).days > _MAX_REPORT_SPAN_DAYS:
        logging.warning(
            "Report window %s→%s exceeds %s days; clamping to current FY for %s",
            start,
            end,
            _MAX_REPORT_SPAN_DAYS,
            company_name or books_from_iso or "company",
        )
        start = current_indian_fy_start(as_on)
        if books_start > start:
            start = books_start
        end = as_on
        if end < start:
            end = start

    return start, end


def iter_company_fy_windows(as_on=None, books_from_iso=None, company_name=None):
    """
    Yield (fy_start, fy_end_or_as_on) for every Indian FY covered by this company.

    Named year companies → only that one FY (same as company_period_bounds).
    Continuous companies → each FY from books_from through the FY containing as_on.
    Each window is ≤ one FY (anti-freeze for Tally exports).
    """
    from datetime import date as date_cls

    if as_on is None:
        as_on = date_cls.today()
    elif isinstance(as_on, datetime):
        as_on = as_on.date()

    if looks_like_year_company_name(company_name):
        start, end = company_period_bounds(as_on, books_from_iso, company_name)
        yield start, end
        return

    books_start = company_period_start(as_on, books_from_iso)
    fy = date_cls(
        books_start.year if books_start.month >= 4 else books_start.year - 1,
        4,
        1,
    )
    current_start = current_indian_fy_start(as_on)
    while fy <= current_start:
        fy_end = indian_fy_end_for(fy)
        end = min(as_on, fy_end)
        if end >= fy:
            yield fy, end
        fy = date_cls(fy.year + 1, 4, 1)


def get_company_voucher_date_ranges(books_from_iso=None):
    """
    Monthly YYYYMMDD chunks covering this company's books through today.

    SYNC_START_YEAR pins one Indian FY for every company.
    Else start at books_from (month start) through the current month.
    If books_from is missing, use the current Indian FY (previous default).
    """
    load_dotenv(override=True)
    custom_year = os.environ.get("SYNC_START_YEAR", "").strip()
    if custom_year and custom_year.isdigit():
        return get_current_fy_monthly_date_ranges()
    if books_from_iso and tally_ymd_to_iso(books_from_iso):
        return get_monthly_date_ranges(tally_ymd_to_iso(books_from_iso))
    return get_current_fy_monthly_date_ranges()


def get_company_item_fetch_window(books_from_iso=None):
    """Single Tally date window spanning the company's voucher ranges (line items)."""
    months = get_company_voucher_date_ranges(books_from_iso)
    if not months:
        return []
    return [(months[0][0], months[-1][1])]

def get_current_fy_item_date_ranges(days_per_chunk=7):
    """
    Returns smaller date chunks (default 7 days) across the financial year
    specifically for heavy inventory line items (sale_bill_item / purchase_bill_item).
    This prevents Tally from freezing or timing out on large invoice volumes.
    """
    load_dotenv(override=True)
    custom_year = os.environ.get("SYNC_START_YEAR", "").strip()

    if custom_year and custom_year.isdigit():
        yr = int(custom_year)
        start_date = datetime(yr, 4, 1)
        end_date = datetime(yr + 1, 3, 31)
    else:
        now = datetime.now()
        if now.month >= 4:
            start_date = datetime(now.year, 4, 1)
        else:
            start_date = datetime(now.year - 1, 4, 1)
        end_date = datetime.now()

    ranges = []
    current = start_date
    while current <= end_date:
        chunk_end = current + timedelta(days=days_per_chunk - 1)
        if chunk_end > end_date:
            chunk_end = end_date
        
        ranges.append((
            current.strftime("%Y%m%d"),
            chunk_end.strftime("%Y%m%d")
        ))
        current = chunk_end + timedelta(days=1)

    return ranges

