import os
import logging
from datetime import datetime, timedelta
from dateutil.relativedelta import relativedelta
from dotenv import load_dotenv

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

