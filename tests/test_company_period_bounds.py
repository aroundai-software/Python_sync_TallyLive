"""Report date windows: continuous vs year companies, anti-freeze span."""
from datetime import date

from src.date_utils import (
    company_period_bounds,
    looks_like_year_company_name,
)


def test_year_company_name_detection():
    assert looks_like_year_company_name("S & D GlobeMate  (2024-2025)")
    assert looks_like_year_company_name("Vadakkethil Sales And Marketing 2026-27")
    assert looks_like_year_company_name("Foo (2025-26)")
    assert not looks_like_year_company_name(
        "SAMAGRA PROGRESSIVE LEARNING SOLUTIONS P LTD"
    )
    assert not looks_like_year_company_name("Vadakkethil Sales And Marketing")


def test_current_fy_company_uses_books_from_to_today():
    as_on = date(2026, 11, 9)
    start, end = company_period_bounds(
        as_on, "2026-04-01", "Vadakkethil Sales And Marketing 2026-27"
    )
    assert start == date(2026, 4, 1)
    assert end == as_on


def test_named_closed_year_company_keeps_that_fy():
    as_on = date(2026, 11, 9)
    start, end = company_period_bounds(
        as_on, "2024-04-01", "S & D GlobeMate  (2024-2025)"
    )
    assert start == date(2024, 4, 1)
    assert end == date(2025, 3, 31)


def test_continuous_old_books_uses_current_fy_only():
    as_on = date(2026, 11, 9)
    start, end = company_period_bounds(
        as_on,
        "2016-04-01",
        "SAMAGRA PROGRESSIVE LEARNING SOLUTIONS P LTD",
    )
    assert start == date(2026, 4, 1)
    assert end == as_on
    assert (end - start).days <= 400


def test_span_never_exceeds_one_fy_plus_slack():
    as_on = date(2026, 11, 9)
    start, end = company_period_bounds(as_on, "2010-04-01", "Some Old Co")
    assert (end - start).days <= 400
    assert start == date(2026, 4, 1)


def test_iter_company_fy_windows_continuous():
    from src.date_utils import iter_company_fy_windows

    as_on = date(2026, 11, 9)
    wins = list(
        iter_company_fy_windows(
            as_on, "2016-04-01", "SAMAGRA PROGRESSIVE LEARNING SOLUTIONS P LTD"
        )
    )
    assert wins[0] == (date(2016, 4, 1), date(2017, 3, 31))
    assert wins[-1] == (date(2026, 4, 1), as_on)
    assert len(wins) == 11
    for a, b in wins:
        assert (b - a).days <= 400


def test_iter_company_fy_windows_named_year():
    from src.date_utils import iter_company_fy_windows

    as_on = date(2026, 11, 9)
    wins = list(
        iter_company_fy_windows(as_on, "2024-04-01", "S & D GlobeMate  (2024-2025)")
    )
    assert wins == [(date(2024, 4, 1), date(2025, 3, 31))]
