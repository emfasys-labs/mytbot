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
    def __init__(self, existing_row=None, realised_scalar=Decimal("0")):
        self.existing_row = existing_row
        self.realised_scalar = realised_scalar
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
            return _Q(scalar=self.realised_scalar)
        return _Q(row=self.existing_row)
    def add(self, row): self.added.append(row)
    async def commit(self): self.commits += 1


def _session_factory(existing_row=None, realised_scalar=Decimal("0")):
    sess = _Session(existing_row=existing_row, realised_scalar=realised_scalar)
    def _factory(): return sess
    _factory.sess = sess
    return _factory


class _FakeReport:
    def __init__(self, *, full): self._full = full
    def coverage(self):
        return {
            "full": self._full,
            # Non-empty so the runtime helper recognises this as a
            # populated report (an empty ``configured`` is treated as the
            # startup window and defaults to "full / no filter").
            "configured": ["ibkr", "kraken"],
            "included": ["kraken"] if not self._full else ["ibkr", "kraken"],
            "excluded": [] if self._full else [{"name": "ibkr"}],
        }


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
async def test_partial_coverage_writes_nav_with_flag():
    # The writer must still persist the live (active-scope) NAV so the
    # dashboard sees a fresh row — but it stamps ``partial_coverage=True``
    # so the HWM reader can skip it.
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
    assert Decimal(str(row.portfolio_value)) == Decimal("255000")
    assert row.strategy_breakdown["partial_coverage"] is True


@pytest.mark.asyncio
async def test_full_then_partial_then_full_in_same_day():
    # A row that was full at noon may flip to partial at 1pm and back to
    # full at 2pm. Each tick reflects its own coverage in the flag and the
    # latest observation in portfolio_value. The HWM reader is responsible
    # for ignoring whichever rows happen to be partial when it runs.
    existing = SimpleNamespace(
        portfolio_value=Decimal("1230000"),
        realised_pnl=Decimal("0"),
        unrealised_pnl=Decimal("0"),
        total_fees=Decimal("0"),
        trade_count=0,
        strategy_breakdown={"partial_coverage": False},
    )
    # Partial tick lands.
    runtime.set_broker_manager(_FakeBM(full=False))
    sf = _session_factory(existing_row=existing)
    await _upsert_daily_pnl(sf, {
        "portfolio_value": Decimal("255000"),
        "trades_today": 7, "consecutive_losses": 0, "cooldown_until": None,
        "daily_loss_accumulated": "0", "positions": {}, "fees_today_delta": Decimal("0"),
    })
    assert existing.portfolio_value == Decimal("255000")
    assert existing.strategy_breakdown["partial_coverage"] is True

    # Full tick lands later — flag clears, NAV updates.
    runtime.set_broker_manager(_FakeBM(full=True))
    sf2 = _session_factory(existing_row=existing)
    await _upsert_daily_pnl(sf2, {
        "portfolio_value": Decimal("1240000"),
        "trades_today": 9, "consecutive_losses": 0, "cooldown_until": None,
        "daily_loss_accumulated": "0", "positions": {}, "fees_today_delta": Decimal("0"),
    })
    assert existing.portfolio_value == Decimal("1240000")
    assert existing.strategy_breakdown["partial_coverage"] is False
    assert existing.trade_count == 9


@pytest.mark.asyncio
async def test_startup_with_unpopulated_report_is_not_partial():
    # Right after orchestrator construction, ``BrokerReport.brokers`` is {}.
    # That is NOT a partial-coverage state — it's just "the report isn't
    # populated yet". The writer should treat it as full so the very first
    # NAV heartbeat doesn't permanently flag today's row as partial.
    class _EmptyReport:
        def coverage(self): return {"full": False, "configured": [], "included": [], "excluded": []}

    class _EmptyBM:
        report = _EmptyReport()

    runtime.set_broker_manager(_EmptyBM())
    sf = _session_factory(existing_row=None)
    state = {
        "portfolio_value": Decimal("100000"),
        "trades_today": 0, "consecutive_losses": 0, "cooldown_until": None,
        "daily_loss_accumulated": "0", "positions": {}, "fees_today_delta": Decimal("0"),
    }
    await _upsert_daily_pnl(sf, state)
    row = sf.sess.added[0]
    assert Decimal(str(row.portfolio_value)) == Decimal("100000")
    assert row.strategy_breakdown["partial_coverage"] is False


@pytest.mark.asyncio
async def test_daily_loss_accumulated_reconciles_from_realised_ledger():
    runtime.set_broker_manager(_FakeBM(full=True))
    sf = _session_factory(existing_row=None, realised_scalar=Decimal("-9107.37"))
    state = {
        "portfolio_value": Decimal("1230000"),
        "trades_today": 34,
        "consecutive_losses": 0,
        "cooldown_until": None,
        "daily_loss_accumulated": "0",
        "positions": {},
        "fees_today_delta": Decimal("0"),
    }

    await _upsert_daily_pnl(sf, state)

    row: DailyPnL = sf.sess.added[0]
    assert row.realised_pnl == Decimal("-9107.37")
    assert row.strategy_breakdown["risk_daily_loss_accumulated"] == "9107.37"
