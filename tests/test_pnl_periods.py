"""Unit tests for api.pnl_periods helpers (no database)."""

import asyncio
from datetime import date
from decimal import Decimal

from api.pnl_periods import (
    PRODUCTION_PNL_START,
    aggregate_daily_pnl_range,
    equity_max_drawdown_pct,
    merge_live_today_unrealised_into_period,
    month_to_date_range,
    week_to_date_range,
)


class _BoomSession:
    """Any DB use is a failure — the all-pre-cutoff path must short-circuit
    BEFORE querying."""

    async def execute(self, *_a, **_k):  # pragma: no cover - must not run
        raise AssertionError("aggregate_daily_pnl_range queried for an all-pre-cutoff window")


def test_rollup_window_entirely_pre_cutoff_is_empty_without_querying() -> None:
    # A month range fully before production start → zeros, no DB hit.
    out = asyncio.run(
        aggregate_daily_pnl_range(_BoomSession(), date(2026, 4, 1), date(2026, 4, 30))
    )
    assert out["realised"] == "0" and out["fees"] == "0" and out["trades"] == 0
    # The reported window start is clamped to the production cutoff.
    assert out["period_start"] == PRODUCTION_PNL_START


def test_production_pnl_start_default() -> None:
    # Default cutoff is the instrumentation date (env-overridable).
    assert PRODUCTION_PNL_START == "2026-05-13"


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
    period = Decimal("100")
    out = merge_live_today_unrealised_into_period(
        period,
        db_today_unrealised=Decimal("40"),
        live_today_unrealised=Decimal("291"),
    )
    assert out == Decimal("291")


def test_merge_live_today_unrealised_keeps_period_when_live_missing():
    out = merge_live_today_unrealised_into_period(
        Decimal("100"),
        db_today_unrealised=Decimal("40"),
        live_today_unrealised=Decimal("0"),
    )
    assert out == Decimal("100")
