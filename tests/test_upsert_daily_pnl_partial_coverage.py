"""
tests/test_upsert_daily_pnl_partial_coverage.py
================================================

Regression for the HWM ratchet bug.

During a coverage gap (e.g. IBKR offline), ``portfolio_state.portfolio_value``
is the active-only NAV. Pre-fix, ``_upsert_daily_pnl`` wrote that smaller
value into ``daily_pnl.portfolio_value``, which feeds the high-watermark
``max(DailyPnL.portfolio_value)`` — so a transient gap could permanently
ratchet the HWM down (or, on first write, permanently down). The fix:
when coverage is partial, the writer leaves ``portfolio_value`` untouched
(or writes 0 on a fresh insert) and stamps ``strategy_breakdown.partial_coverage``.
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest

import control.runtime as runtime
from run_m3 import _upsert_daily_pnl
from storage.models import DailyPnL


class _Q:
    def __init__(self, row=None, scalar=0):
        self._row = row
        self._scalar = scalar
    def scalars(self):
        return SimpleNamespace(first=lambda: self._row, all=lambda: ([self._row] if self._row else []))
    def scalar_one(self): return self._scalar
    def scalar_one_or_none(self): return self._scalar


class _Session:
    def __init__(self, existing_row=None):
        self.existing_row = existing_row
        self.added: list = []
        self.commits = 0
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def execute(self, stmt):
        # Three callsites in _upsert_daily_pnl after our fix:
        #   1) _compute_today_realised_pnl → SUM(FillLog.realised_pnl) → scalar_one
        #   2) SELECT DailyPnL WHERE date = ...
        text = str(stmt)
        if "fills" in text.lower() or "realised_pnl" in text.lower() and "daily_pnl" not in text.lower():
            return _Q(scalar=Decimal("0"))
        return _Q(row=self.existing_row)
    def add(self, row): self.added.append(row)
    async def commit(self): self.commits += 1


def _session_factory(existing_row=None):
    sess = _Session(existing_row=existing_row)
    def _factory(): return sess
    _factory.sess = sess
    return _factory


class _FakeReport:
    def __init__(self, *, full): self._full = full
    def coverage(self): return {"full": self._full, "included": [], "excluded": []}


class _FakeBM:
    def __init__(self, *, full): self.report = _FakeReport(full=full)


@pytest.fixture(autouse=True)
def _reset_runtime():
    saved = runtime.get_broker_manager()
    yield
    runtime.set_broker_manager(saved)


@pytest.mark.asyncio
async def test_full_coverage_writes_portfolio_value():
    runtime.set_broker_manager(_FakeBM(full=True))
    sf = _session_factory(existing_row=None)
    state = {
        "portfolio_value": Decimal("1230000"),
        "trades_today": 5,
        "consecutive_losses": 0,
        "cooldown_until": None,
        "daily_loss_accumulated": "0",
        "positions": {},
        "fees_today_delta": Decimal("0"),
    }
    await _upsert_daily_pnl(sf, state)
    assert len(sf.sess.added) == 1
    row: DailyPnL = sf.sess.added[0]
    assert Decimal(str(row.portfolio_value)) == Decimal("1230000")
    assert row.strategy_breakdown["partial_coverage"] is False


@pytest.mark.asyncio
async def test_partial_coverage_does_not_write_partial_nav():
    # Fresh insert: no prior row exists. Under partial coverage the writer
    # must NOT seed the row with the active-only NAV (which would
    # immediately establish an artificially low HWM).
    runtime.set_broker_manager(_FakeBM(full=False))
    sf = _session_factory(existing_row=None)
    state = {
        "portfolio_value": Decimal("255000"),  # IBKR-excluded NAV
        "trades_today": 5,
        "consecutive_losses": 0,
        "cooldown_until": None,
        "daily_loss_accumulated": "0",
        "positions": {},
        "fees_today_delta": Decimal("0"),
    }
    await _upsert_daily_pnl(sf, state)
    row: DailyPnL = sf.sess.added[0]
    assert Decimal(str(row.portfolio_value)) == Decimal("0")
    assert row.strategy_breakdown["partial_coverage"] is True


@pytest.mark.asyncio
async def test_partial_coverage_preserves_existing_portfolio_value():
    # An earlier full-coverage tick wrote portfolio_value = 1.23M. A later
    # partial-coverage tick must NOT overwrite it with the active-only NAV.
    existing = SimpleNamespace(
        portfolio_value=Decimal("1230000"),
        realised_pnl=Decimal("0"),
        unrealised_pnl=Decimal("0"),
        total_fees=Decimal("0"),
        trade_count=0,
        strategy_breakdown={},
    )
    runtime.set_broker_manager(_FakeBM(full=False))
    sf = _session_factory(existing_row=existing)
    state = {
        "portfolio_value": Decimal("255000"),
        "trades_today": 7,
        "consecutive_losses": 0,
        "cooldown_until": None,
        "daily_loss_accumulated": "0",
        "positions": {},
        "fees_today_delta": Decimal("0"),
    }
    await _upsert_daily_pnl(sf, state)
    assert existing.portfolio_value == Decimal("1230000")
    assert existing.trade_count == 7  # non-NAV fields still update
    assert existing.strategy_breakdown["partial_coverage"] is True
