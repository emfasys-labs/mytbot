from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from brokers.base import OrderBook, OrderResult, OrderStatus, OrderSide
from control.runtime import set_risk_engine
from execution.engine import ExecutionEngine
from risk.engine import RiskDecision, RiskVerdict, Signal


@dataclass
class _FakeRiskEngine:
    config: dict
    killed: bool = False

    def kill(self) -> None:
        self.killed = True


class _FakeBroker:
    def __init__(self, *, connect_ok: bool = True, fail_place: bool = False, wide_spread: bool = False):
        self.connect_ok = connect_ok
        self.fail_place = fail_place
        self.wide_spread = wide_spread
        self.place_calls = 0

    async def connect(self) -> bool:
        return self.connect_ok

    async def place_order(self, order):
        self.place_calls += 1
        if self.fail_place:
            raise RuntimeError("broker down")
        return OrderResult(
            broker_order_id="b-1",
            client_order_id=order.client_order_id,
            status=OrderStatus.OPEN,
            symbol=order.symbol,
            side=order.side,
            quantity=order.quantity,
            filled_quantity=Decimal("0"),
            avg_fill_price=None,
            fee=None,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

    async def get_order_book(self, symbol: str, depth: int = 10) -> OrderBook:
        if self.wide_spread:
            bids = [(Decimal("100"), Decimal("1000"))]
            asks = [(Decimal("102"), Decimal("1000"))]  # 1.98% spread
        else:
            bids = [(Decimal("100"), Decimal("10000"))]
            asks = [(Decimal("100.1"), Decimal("10000"))]
        return OrderBook(
            symbol=symbol,
            timestamp=datetime.now(timezone.utc).isoformat(),
            bids=bids,
            asks=asks,
        )

    async def get_open_orders(self):
        return []

    async def cancel_order(self, broker_order_id: str):
        return True

    async def get_positions(self):
        return [
            SimpleNamespace(symbol="SPY", quantity=Decimal("2"))
        ]


def _approved_decision() -> RiskDecision:
    return RiskDecision(
        verdict=RiskVerdict.APPROVED,
        reason="ok",
        signal_id="s-1",
        checks_passed=["x"],
        checks_failed=[],
    )


def _signal() -> Signal:
    return Signal(
        signal_id="s-1",
        symbol="SPY",
        side="buy",
        strategy="momentum_breakout",
        confidence=0.9,
        suggested_quantity=Decimal("10"),
        suggested_price=None,
        broker="ibkr",
        asset_class="equity",
        timestamp=datetime.now(timezone.utc).isoformat(),
        metadata={},
    )


@pytest.mark.asyncio
async def test_auto_kill_on_api_failure(monkeypatch) -> None:
    risk = _FakeRiskEngine({"auto_kill_on_api_failure": True})
    set_risk_engine(risk)
    broker = _FakeBroker(fail_place=True)
    monkeypatch.setattr("execution.engine.get_broker", lambda *args, **kwargs: broker)

    engine = ExecutionEngine(broker_configs={}, paper_mode=True)
    result = await engine.execute(_signal(), _approved_decision())
    assert result is None
    assert risk.killed is True


@pytest.mark.asyncio
async def test_rejects_on_spread_precheck(monkeypatch) -> None:
    risk = _FakeRiskEngine(
        {
            "max_spread_pct": Decimal("0.003"),
            "min_liquidity_usd": Decimal("1"),
            "max_slippage_pct": Decimal("1"),
            "auto_kill_on_api_failure": False,
        }
    )
    set_risk_engine(risk)
    broker = _FakeBroker(wide_spread=True)
    monkeypatch.setattr("execution.engine.get_broker", lambda *args, **kwargs: broker)

    engine = ExecutionEngine(broker_configs={}, paper_mode=True)
    result = await engine.execute(_signal(), _approved_decision())
    assert result is None
    assert broker.place_calls == 0


@pytest.mark.asyncio
async def test_places_order_when_execution_checks_pass(monkeypatch) -> None:
    risk = _FakeRiskEngine(
        {
            "max_spread_pct": Decimal("0.05"),
            "min_liquidity_usd": Decimal("1000"),
            "max_slippage_pct": Decimal("0.05"),
            "auto_kill_on_api_failure": False,
        }
    )
    set_risk_engine(risk)
    broker = _FakeBroker()
    monkeypatch.setattr("execution.engine.get_broker", lambda *args, **kwargs: broker)

    engine = ExecutionEngine(broker_configs={}, paper_mode=True)
    result = await engine.execute(_signal(), _approved_decision())
    assert result is not None
    assert broker.place_calls == 1
    assert risk.killed is False


@pytest.mark.asyncio
async def test_reconcile_positions_mismatch_auto_kills(monkeypatch) -> None:
    risk = _FakeRiskEngine(
        {
            "auto_kill_on_reconciliation_failure": True,
            "auto_kill_on_api_failure": False,
            "max_spread_pct": Decimal("1"),
            "min_liquidity_usd": Decimal("0"),
            "max_slippage_pct": Decimal("1"),
        }
    )
    set_risk_engine(risk)
    broker = _FakeBroker()
    monkeypatch.setattr("execution.engine.get_broker", lambda *args, **kwargs: broker)

    class _FakeScalarResult:
        def __init__(self, value):
            self._value = value

        def scalar_one_or_none(self):
            return self._value

        def scalars(self):
            return self

        def all(self):
            return []

    class _FakeSession:
        async def execute(self, _stmt):
            # max timestamp exists, but no local rows -> mismatch vs remote qty=2
            return _FakeScalarResult(datetime.now(timezone.utc))

    class _FakeFactory:
        def __call__(self):
            s = _FakeSession()

            class _CM:
                async def __aenter__(self_inner):
                    return s

                async def __aexit__(self_inner, exc_type, exc, tb):
                    return False

            return _CM()

    async def _fake_init_db():
        return object(), _FakeFactory()

    async def _fake_dispose(_engine):
        return None

    monkeypatch.setattr(ExecutionEngine, "_init_db", staticmethod(_fake_init_db))
    monkeypatch.setattr(ExecutionEngine, "_dispose_db", staticmethod(_fake_dispose))

    engine = ExecutionEngine(broker_configs={}, paper_mode=True)
    # prime broker cache
    await engine._get_broker("ibkr")
    ok = await engine.reconcile_positions()
    assert ok is False
    assert risk.killed is True

