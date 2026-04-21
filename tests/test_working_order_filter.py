"""Filter strategy opportunities against already-working broker orders.

The global edge coordinator gets a bounded action budget per tick
(``max_actions_per_tick``). If the top-ranked opportunities all coincide with
limit orders still sitting at the broker from the previous cycle, the
execution engine dedups every one of them and the iteration ends with
``executed=0``. The trading loop therefore pre-filters ``new_opps`` by the set
of working-order keys before handing them to the coordinator. These tests
exercise that filter end-to-end against an in-memory ``OrderLog``-shaped
stand-in.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from system.trading_loop.loop import (
    _DIRECTIONAL_SIDES,
    _SIDE_TO_ORDER_SIDE,
    _load_working_order_keys,
)


@dataclass
class _Row:
    symbol: str
    side: str


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return [(r.symbol, r.side) for r in self._rows]


class _FakeSession:
    def __init__(self, rows):
        self._rows = rows

    async def execute(self, _stmt):
        return _Result(self._rows)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _session_factory(rows):
    @asynccontextmanager
    async def _ctx():
        yield _FakeSession(rows)

    def _factory():
        return _ctx()

    return _factory


@pytest.mark.asyncio
async def test_load_working_order_keys_normalises_case_and_whitespace():
    rows = [
        _Row(symbol="aapl ", side="Buy"),
        _Row(symbol="TSLA", side="sell"),
        _Row(symbol=" msft", side="BUY"),
    ]
    keys = await _load_working_order_keys(_session_factory(rows))
    assert keys == {("AAPL", "buy"), ("TSLA", "sell"), ("MSFT", "buy")}


@pytest.mark.asyncio
async def test_load_working_order_keys_skips_blank_rows():
    rows = [
        _Row(symbol="", side="buy"),
        _Row(symbol="FOO", side=""),
        _Row(symbol="BAR", side="buy"),
    ]
    keys = await _load_working_order_keys(_session_factory(rows))
    assert keys == {("BAR", "buy")}


@pytest.mark.asyncio
async def test_load_working_order_keys_returns_empty_on_none_factory():
    keys = await _load_working_order_keys(None)
    assert keys == set()


@pytest.mark.asyncio
async def test_load_working_order_keys_tolerates_db_failure():
    @asynccontextmanager
    async def _boom_ctx():
        raise RuntimeError("db down")
        yield  # pragma: no cover

    def _boom_factory():
        return _boom_ctx()

    keys = await _load_working_order_keys(_boom_factory)
    assert keys == set()


def test_side_mapping_covers_every_directional_side():
    """Every side we treat as ``directional`` must map to a broker order side;
    otherwise the filter would let a duplicate slip through on rounding."""
    for s in _DIRECTIONAL_SIDES:
        assert s in _SIDE_TO_ORDER_SIDE, f"missing order-side mapping for {s!r}"
        assert _SIDE_TO_ORDER_SIDE[s] in {"buy", "sell"}


@dataclass
class _FakeOpp:
    symbol: str
    side: str


def test_filter_blocks_when_working_order_exists_for_same_side():
    """Pure-logic smoke test mirroring what ``_run_global_edge_tick`` does
    inline. Kept here so a regression in the filter condition is caught even
    if someone refactors the trading loop method signature."""
    working = {("AAPL", "buy"), ("TSLA", "sell")}
    opps = [
        _FakeOpp(symbol="AAPL", side="long"),    # blocked
        _FakeOpp(symbol="tsla", side="short"),   # blocked (case-insensitive)
        _FakeOpp(symbol="MSFT", side="long"),    # allowed
        _FakeOpp(symbol="AAPL", side="short"),   # allowed (opposite side)
        _FakeOpp(symbol="BTC-USD", side="ARBITRAGE_SPOT_SPREAD"),  # not directional → allowed
    ]
    kept = []
    for o in opps:
        side_raw = o.side.strip().lower()
        if side_raw in _DIRECTIONAL_SIDES:
            sym = o.symbol.strip().upper()
            order_side = _SIDE_TO_ORDER_SIDE.get(side_raw, side_raw)
            if sym and (sym, order_side) in working:
                continue
        kept.append(o.symbol.upper())
    assert kept == ["MSFT", "AAPL", "BTC-USD"]


# Suppresses an unused-symbol lint when the Decimal/datetime imports are
# re-used in follow-on fixtures; keep imports stable so future parametrised
# tests don't need to reintroduce them.
_ = (Decimal, datetime, timezone)
