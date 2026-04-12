"""Unit tests for api.pnl_periods helpers (no database)."""

from datetime import date
from decimal import Decimal

from api.pnl_periods import (
    equity_max_drawdown_pct,
    merge_live_today_unrealised_into_period,
    month_to_date_range,
    week_to_date_range,
)


def test_week_to_date_range_monday_through_today():
    d = date(2026, 4, 12)  # Sunday
    start, end = week_to_date_range(d)
    assert start == date(2026, 4, 6)
    assert end == d


def test_month_to_date_range():
    d = date(2026, 4, 12)
    start, end = month_to_date_range(d)
    assert start == date(2026, 4, 1)
    assert end == d


def test_equity_max_drawdown_pct():
    dd = equity_max_drawdown_pct(
        [Decimal("100"), Decimal("120"), Decimal("80"), Decimal("90"), Decimal("100")]
    )
    assert dd is not None
    assert dd > 0


def test_equity_max_drawdown_short_series_returns_none():
    assert equity_max_drawdown_pct([Decimal("1"), Decimal("2")]) is None


def test_merge_live_today_unrealised_into_period():
    period = Decimal("100")  # includes DB today unrealised 40
    out = merge_live_today_unrealised_into_period(
        period,
        db_today_unrealised=Decimal("40"),
        live_today_unrealised=Decimal("291"),
    )
    assert out == Decimal("351")
