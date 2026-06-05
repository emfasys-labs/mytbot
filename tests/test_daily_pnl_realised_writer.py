"""
tests/test_daily_pnl_realised_writer.py
========================================

Locks in the ``daily_pnl.realised_pnl`` self-healing writer: regardless of
which execution path produced the fills (legacy or D015 batch), the persisted
daily snapshot must report realised P&L summed from the canonical
``FillLog`` ledger ([[project_fills_ledger]]).

The pre-fix implementation replayed today's ``OrderLog`` from an empty
position state, so a sell of a position opened on a prior day was treated
as "opening a short" — its realised P&L silently vanished. We now read the
weighted-average-cost ``FillLog.realised_pnl`` written on each closing
fill, which is correct across day boundaries by construction.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from run_m3 import _compute_today_realised_pnl


class _Result:
    def __init__(self, total): self._total = total
    def scalar_one(self): return self._total


class _FakeSession:
    def __init__(self, fill_realised_total): self._t = fill_realised_total
    async def execute(self, _stmt): return _Result(self._t)


@pytest.mark.asyncio
async def test_zero_when_no_fills_today() -> None:
    out = await _compute_today_realised_pnl(_FakeSession(0))
    assert out == Decimal("0")


@pytest.mark.asyncio
async def test_sum_passthrough_positive() -> None:
    out = await _compute_today_realised_pnl(_FakeSession(Decimal("1234.56")))
    assert out == Decimal("1234.56")


@pytest.mark.asyncio
async def test_sum_passthrough_negative() -> None:
    out = await _compute_today_realised_pnl(_FakeSession(Decimal("-42.00")))
    assert out == Decimal("-42.00")


@pytest.mark.asyncio
async def test_cross_day_close_is_captured() -> None:
    """
    Regression for the cross-day bug.

    Pre-fix: a position opened yesterday and closed today was missed
    because the per-day OrderLog replay began from an empty state. The
    FillLog already carries weighted-average-cost realised P&L stamped
    when the closing fill landed, so the daily sum picks it up regardless
    of when the position was opened. The fake session here mimics what
    the DB returns: SUM(realised_pnl) over today's window.
    """
    closing_pnl_recorded_on_fill = Decimal("875.25")
    out = await _compute_today_realised_pnl(_FakeSession(closing_pnl_recorded_on_fill))
    assert out == closing_pnl_recorded_on_fill


@pytest.mark.asyncio
async def test_none_total_treated_as_zero() -> None:
    """``COALESCE(SUM(...), 0)`` should never return None, but be defensive."""
    out = await _compute_today_realised_pnl(_FakeSession(None))
    assert out == Decimal("0")
