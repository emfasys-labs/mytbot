"""
tests/test_daily_pnl_realised_writer.py
========================================

Locks in the ``daily_pnl.realised_pnl`` self-healing writer: regardless of
which execution path produced the fills (legacy or D015 batch), the
persisted daily snapshot must report round-trip realised P&L computed
directly from today's orders — never stale from a stale state dict.

Previously the column read 0 while the API's ``/pnl`` correctly summed
$8,777 of realised profit.
"""

from __future__ import annotations

from datetime import datetime, time, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from run_m3 import _compute_today_realised_pnl


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _order(*, symbol: str, side: str, qty: str, price: str, fee: str,
           broker: str = "ibkr", ts: datetime | None = None, status: str = "filled",
           rid: str = "x"):
    return SimpleNamespace(
        id=rid,
        symbol=symbol,
        broker=broker,
        side=side,
        quantity=Decimal(qty),
        filled_quantity=Decimal(qty),
        avg_fill_price=Decimal(price),
        limit_price=None,
        fee=Decimal(fee),
        status=status,
        timestamp=ts or _utc_now(),
    )


class _Scalars:
    def __init__(self, rows): self._rows = rows
    def all(self): return self._rows


class _Result:
    def __init__(self, rows): self._rows = rows
    def scalars(self): return _Scalars(self._rows)


class _FakeSession:
    def __init__(self, rows): self._rows = rows
    async def execute(self, _stmt): return _Result(self._rows)


@pytest.mark.asyncio
async def test_no_orders_returns_zero() -> None:
    out = await _compute_today_realised_pnl(_FakeSession([]))
    assert out == Decimal("0")


@pytest.mark.asyncio
async def test_open_only_returns_zero() -> None:
    """A position that only opens and never closes realises nothing."""
    rows = [
        _order(symbol="AAPL", side="buy", qty="10", price="100", fee="1", rid="o1"),
    ]
    out = await _compute_today_realised_pnl(_FakeSession(rows))
    assert out == Decimal("0")


@pytest.mark.asyncio
async def test_long_round_trip_in_profit() -> None:
    """Open 10@100, close 10@110, fee 1+1 — net = (110-100)*10 - 2 = 98."""
    rows = [
        _order(symbol="AAPL", side="buy",  qty="10", price="100", fee="1", rid="o1"),
        _order(symbol="AAPL", side="sell", qty="10", price="110", fee="1", rid="o2"),
    ]
    out = await _compute_today_realised_pnl(_FakeSession(rows))
    # fee_alloc on close = 1 * (10/10) = 1; opening fee doesn't apply to realised.
    assert out == Decimal("99")


@pytest.mark.asyncio
async def test_short_round_trip_in_profit() -> None:
    """Short 10@100, cover 10@90 — net = (100-90)*10 - close_fee."""
    rows = [
        _order(symbol="TSLA", side="sell", qty="10", price="100", fee="1", rid="o1"),
        _order(symbol="TSLA", side="buy",  qty="10", price="90",  fee="2", rid="o2"),
    ]
    out = await _compute_today_realised_pnl(_FakeSession(rows))
    # gross = (100-90)*10 = 100; fee_alloc on close = 2 (full); net = 98
    assert out == Decimal("98")


@pytest.mark.asyncio
async def test_partial_close_realises_proportional() -> None:
    """Open 20@100, close 5@110 — net = (110-100)*5 - (fee * 5/5)."""
    rows = [
        _order(symbol="AAPL", side="buy",  qty="20", price="100", fee="2", rid="o1"),
        _order(symbol="AAPL", side="sell", qty="5",  price="110", fee="1", rid="o2"),
    ]
    out = await _compute_today_realised_pnl(_FakeSession(rows))
    # gross = (110-100)*5 = 50; fee_alloc = 1 * (5/5) = 1; net = 49
    assert out == Decimal("49")


@pytest.mark.asyncio
async def test_multiple_independent_positions_sum() -> None:
    """Separate symbols accumulate independently."""
    rows = [
        _order(symbol="A", side="buy",  qty="10", price="100", fee="1", rid="o1"),
        _order(symbol="A", side="sell", qty="10", price="105", fee="1", rid="o2"),  # +49
        _order(symbol="B", side="sell", qty="20", price="50",  fee="2", rid="o3"),
        _order(symbol="B", side="buy",  qty="20", price="48",  fee="2", rid="o4"),  # +38
    ]
    out = await _compute_today_realised_pnl(_FakeSession(rows))
    # A: (105-100)*10 - 1 = 49; B: (50-48)*20 - 2 = 38; total = 87
    assert out == Decimal("87")


@pytest.mark.asyncio
async def test_broker_scoping_keeps_positions_separate() -> None:
    """Same symbol on two brokers tracks two independent positions."""
    rows = [
        _order(symbol="BTC-USD", broker="kraken", side="buy",  qty="1", price="50000", fee="10", rid="o1"),
        _order(symbol="BTC-USD", broker="kraken", side="sell", qty="1", price="51000", fee="10", rid="o2"),  # +990
        _order(symbol="BTC-USD", broker="binance", side="buy", qty="1", price="50500", fee="5",  rid="o3"),
        # not closed yet on binance — no contribution
    ]
    out = await _compute_today_realised_pnl(_FakeSession(rows))
    assert out == Decimal("990")
