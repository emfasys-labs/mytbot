"""
tests/test_load_portfolio_state_partial_coverage.py
====================================================

Regression for the partial-coverage accounting fix.

When a configured broker (e.g. IBKR) is disconnected, ``live_portfolio_snapshot``
returns the active-only NAV. Pre-fix, ``_load_portfolio_state`` still summed
``PositionLog`` across every broker into ``current_gross_exposure``, so the
risk engine paired a full-book numerator with the active-only denominator
and saw inflated leverage. Now those out-of-scope positions are kept in a
``offline_exposure`` sidecar (so hard rails can still consult them) but
excluded from ``current_gross_exposure`` / ``positions`` for ratio
computations.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

import control.runtime as runtime
from run_m3 import _load_portfolio_state


class _Q:
    def __init__(self, rows=(), scalar=None):
        self._rows = list(rows)
        self._scalar = scalar
    def scalars(self):
        return SimpleNamespace(first=lambda: (self._rows[0] if self._rows else None),
                               all=lambda: list(self._rows))
    def scalar_one_or_none(self):
        return self._scalar
    def scalar_one(self):
        return self._scalar
    def one(self):
        return (self._scalar,)


class _Session:
    def __init__(self, position_rows):
        self._position_rows = position_rows
        self._step = 0
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def execute(self, _stmt):
        self._step += 1
        # Caller order in _load_portfolio_state:
        #   1) latest DailyPnL row
        #   2) max(DailyPnL.portfolio_value)
        #   3) latest PositionLog rows
        #   4) trades_today (count OrderLog)
        if self._step == 1:
            return _Q(rows=[])
        if self._step == 2:
            return _Q(scalar=None)
        if self._step == 3:
            return _Q(rows=self._position_rows)
        return _Q(scalar=0)


def _session_factory(position_rows):
    def _factory(): return _Session(position_rows)
    return _factory


def _pos(symbol, broker, qty, price, asset_class="equity"):
    return SimpleNamespace(
        symbol=symbol,
        broker=broker,
        quantity=Decimal(str(qty)),
        avg_entry_price=Decimal(str(price)),
        current_price=Decimal(str(price)),
        unrealised_pnl=Decimal("0"),
        asset_class=asset_class,
        instrument_metadata=None,
        timestamp=datetime.now(timezone.utc),
    )


class _FakeReport:
    def __init__(self, *, full: bool, included: list[str], configured: list[str] | None = None):
        cfg = list(configured) if configured is not None else (
            list(included) + (["ibkr"] if not full and "ibkr" not in included else [])
        )
        self._cov = {
            "full": full,
            "configured": cfg,
            "included": list(included),
            "excluded": [{"name": n} for n in cfg if n not in included],
        }
    def coverage(self): return self._cov


class _FakeBM:
    def __init__(self, *, full: bool, included: list[str], configured: list[str] | None = None):
        self.report = _FakeReport(full=full, included=included, configured=configured)


@pytest.fixture(autouse=True)
def _reset_runtime():
    saved = runtime.get_broker_manager()
    yield
    runtime.set_broker_manager(saved)


@pytest.mark.asyncio
async def test_full_coverage_includes_every_broker():
    runtime.set_broker_manager(_FakeBM(full=True, included=["ibkr", "kraken"]))
    rows = [_pos("AAPL", "ibkr", 10, 100), _pos("BTC-USD", "kraken", 1, 50_000)]
    sf = _session_factory(rows)
    state = await _load_portfolio_state(sf, fallback_portfolio_value=Decimal("100000"))
    # 10*100 + 1*50_000 = 51_000 — all brokers in scope
    assert state["current_gross_exposure"] == Decimal("51000")
    assert state["offline_exposure"] == Decimal("0")
    assert state["offline_brokers"] == []
    assert state["coverage_partial"] is False
    assert set(p["broker"] for p in state["positions"].values()) == {"ibkr", "kraken"}


@pytest.mark.asyncio
async def test_partial_coverage_partitions_exposure():
    # Active scope excludes IBKR (gateway dropped). Kraken position contributes
    # to current_gross_exposure; the IBKR position becomes offline_exposure.
    runtime.set_broker_manager(_FakeBM(full=False, included=["kraken"]))
    rows = [_pos("AAPL", "ibkr", 10, 100), _pos("BTC-USD", "kraken", 1, 50_000)]
    sf = _session_factory(rows)
    state = await _load_portfolio_state(sf, fallback_portfolio_value=Decimal("100000"))
    assert state["current_gross_exposure"] == Decimal("50000")
    assert state["offline_exposure"] == Decimal("1000")
    assert state["offline_brokers"] == ["ibkr"]
    assert state["coverage_partial"] is True
    # AAPL is offline — it must NOT appear in the active-scope positions dict
    # (which feeds the risk engine's leverage ratio).
    brokers_in_scope = {p["broker"] for p in state["positions"].values()}
    assert brokers_in_scope == {"kraken"}


@pytest.mark.asyncio
async def test_startup_with_empty_report_does_not_flag_offline():
    # During the orchestrator-init → discover_and_connect window the broker
    # manager exists but ``report.brokers`` is empty. That must NOT be read
    # as "every broker is offline" — otherwise every persisted position
    # would be moved into offline_exposure and current_gross_exposure would
    # zero out, making the trading loop behave as if the book were empty.
    class _EmptyReport:
        def coverage(self): return {"full": False, "configured": [], "included": [], "excluded": []}

    class _EmptyBM:
        report = _EmptyReport()

    runtime.set_broker_manager(_EmptyBM())
    rows = [_pos("AAPL", "ibkr", 10, 100), _pos("BTC-USD", "kraken", 1, 50_000)]
    sf = _session_factory(rows)
    state = await _load_portfolio_state(sf, fallback_portfolio_value=Decimal("100000"))
    assert state["current_gross_exposure"] == Decimal("51000")
    assert state["offline_exposure"] == Decimal("0")
    assert state["coverage_partial"] is False


@pytest.mark.asyncio
async def test_offline_positions_do_not_inflate_symbol_exposure():
    runtime.set_broker_manager(_FakeBM(full=False, included=["kraken"]))
    rows = [
        _pos("BTC-USD", "ibkr", 2, 50_000),     # offline
        _pos("BTC-USD", "kraken", 1, 50_000),   # active
    ]
    sf = _session_factory(rows)
    state = await _load_portfolio_state(sf, fallback_portfolio_value=Decimal("100000"))
    # Active-side BTC-USD exposure only; the IBKR leg lives in offline_exposure.
    assert state["symbol_exposure"]["BTC-USD"] == Decimal("50000")
    assert state["offline_exposure"] == Decimal("100000")
