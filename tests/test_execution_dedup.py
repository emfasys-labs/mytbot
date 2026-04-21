"""Execution-engine dedup for in-flight orders.

When the allocator re-ranks the same opportunity on consecutive loops we must
not flood the broker book with duplicate limit orders. These tests cover the
``_find_in_flight_order`` query logic and the ``execute()`` short-circuit.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from control.runtime import set_risk_engine
from execution.engine import ExecutionEngine
from risk.engine import RiskDecision, RiskVerdict, Signal


@dataclass
class _FakeRiskEngine:
    config: dict

    def kill(self) -> None:  # pragma: no cover
        pass

    def disable_broker(self, name: str) -> None:  # pragma: no cover
        pass

    def reset_kill(self) -> None:  # pragma: no cover
        pass


@dataclass
class _StoredOrder:
    id: str
    symbol: str
    broker: str
    side: str
    status: str
    quantity: Decimal
    timestamp: datetime


class _FakeSession:
    """Minimal async session that returns a configured set of OrderLog rows
    for any SELECT. The real SQLAlchemy expression is not inspected — we
    trust the engine to pass a properly-filtered SELECT and assert the
    short-circuit behaviour from the call/no-call perspective."""

    def __init__(self, rows):
        self._rows = rows

    async def execute(self, _stmt):
        rows = list(self._rows)

        class _Scalars:
            def __init__(self, rs):
                self._rs = rs

            def first(self):
                return self._rs[0] if self._rs else None

            def all(self):
                return list(self._rs)

        class _Result:
            def __init__(self, rs):
                self._rs = rs

            def scalars(self):
                return _Scalars(self._rs)

        return _Result(rows)

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


def _signal(symbol="FUTY", side="buy", broker="ibkr") -> Signal:
    return Signal(
        signal_id=f"sig-{symbol}",
        symbol=symbol,
        side=side,
        strategy="mean_reversion",
        confidence=0.7,
        suggested_quantity=Decimal("100"),
        suggested_price=Decimal("58.24"),
        broker=broker,
        asset_class="equity",
        timestamp=datetime.now(timezone.utc).isoformat(),
        metadata={},
    )


def _approved() -> RiskDecision:
    return RiskDecision(
        verdict=RiskVerdict.APPROVED,
        reason="ok",
        signal_id="sig",
        checks_passed=["x"],
        checks_failed=[],
    )


@pytest.mark.asyncio
async def test_dedup_skips_when_recent_pending_order_exists(monkeypatch):
    set_risk_engine(_FakeRiskEngine({"auto_kill_on_api_failure": False}))
    # Place-order must not be reached — if dedup fails, the test will fail by
    # attempting to look up a broker we didn't configure.
    monkeypatch.setattr("execution.engine.get_broker", lambda *a, **kw: None)

    existing = _StoredOrder(
        id="existing-1",
        symbol="FUTY",
        broker="ibkr",
        side="buy",
        status="pending",
        quantity=Decimal("1519"),
        timestamp=datetime.now(timezone.utc) - timedelta(minutes=2),
    )
    sf = _session_factory([existing])

    engine = ExecutionEngine(broker_configs={}, paper_mode=True)
    result = await engine.execute(_signal(), _approved(), session_factory=sf)

    assert result is None
    assert engine.dedup_skipped == 1


@pytest.mark.asyncio
async def test_dedup_allows_when_no_in_flight_order(monkeypatch):
    set_risk_engine(_FakeRiskEngine({"auto_kill_on_api_failure": False}))
    # With no broker and paper_mode=True, execute falls through to the
    # simulated-fill path. We just assert dedup did not short-circuit.
    monkeypatch.setattr("execution.engine.get_broker", lambda *a, **kw: None)
    sf = _session_factory([])  # nothing in-flight

    engine = ExecutionEngine(broker_configs={}, paper_mode=True)
    result = await engine.execute(_signal(), _approved(), session_factory=sf)

    assert engine.dedup_skipped == 0
    # Paper sim should produce a fill when no broker is configured.
    assert result is not None


@pytest.mark.asyncio
async def test_dedup_disabled_when_window_is_zero(monkeypatch):
    set_risk_engine(_FakeRiskEngine({"auto_kill_on_api_failure": False}))
    monkeypatch.setattr("execution.engine.get_broker", lambda *a, **kw: None)
    monkeypatch.setenv("EXECUTION_DEDUP_WINDOW_SEC", "0")

    existing = _StoredOrder(
        id="existing-1",
        symbol="FUTY",
        broker="ibkr",
        side="buy",
        status="pending",
        quantity=Decimal("1519"),
        timestamp=datetime.now(timezone.utc),
    )
    sf = _session_factory([existing])

    engine = ExecutionEngine(broker_configs={}, paper_mode=True)
    result = await engine.execute(_signal(), _approved(), session_factory=sf)

    # With window=0, dedup is bypassed and the signal is executed.
    assert engine.dedup_skipped == 0
    assert result is not None


@pytest.mark.asyncio
async def test_dedup_tolerates_db_failure(monkeypatch):
    """A DB hiccup must not block trading — fall through to placement."""
    set_risk_engine(_FakeRiskEngine({"auto_kill_on_api_failure": False}))
    monkeypatch.setattr("execution.engine.get_broker", lambda *a, **kw: None)

    @asynccontextmanager
    async def _boom_ctx():
        raise RuntimeError("db down")
        yield  # pragma: no cover

    def _boom_factory():
        return _boom_ctx()

    engine = ExecutionEngine(broker_configs={}, paper_mode=True)
    result = await engine.execute(_signal(), _approved(), session_factory=_boom_factory)

    assert engine.dedup_skipped == 0
    assert result is not None  # paper fallthrough


@pytest.mark.asyncio
async def test_dedup_requires_session_factory(monkeypatch):
    """No session_factory means no DB access; we fall through gracefully."""
    set_risk_engine(_FakeRiskEngine({"auto_kill_on_api_failure": False}))
    monkeypatch.setattr("execution.engine.get_broker", lambda *a, **kw: None)

    engine = ExecutionEngine(broker_configs={}, paper_mode=True)
    result = await engine.execute(_signal(), _approved(), session_factory=None)

    assert engine.dedup_skipped == 0
    assert result is not None
