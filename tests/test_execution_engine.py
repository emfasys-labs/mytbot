from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from brokers.base import (
    AssetClass,
    Order,
    OrderBook,
    OrderResult,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
)
from control.runtime import set_risk_engine
from execution.engine import ExecutionEngine
from risk.engine import RiskDecision, RiskVerdict, Signal


@dataclass
class _FakeRiskEngine:
    config: dict
    killed: bool = False
    disabled: set[str] = field(default_factory=set)

    def kill(self) -> None:
        self.killed = True

    def disable_broker(self, name: str) -> None:
        self.disabled.add(str(name).strip().lower())

    def reset_kill(self) -> None:
        self.killed = False
        self.disabled.clear()


class _FakeBroker:
    def __init__(
        self,
        *,
        connect_ok: bool = True,
        fail_place: bool = False,
        wide_spread: bool = False,
        low_liquidity: bool = False,
        thin_book: bool = False,
        order_status_sequence: list[OrderStatus] | None = None,
    ):
        self.connect_ok = connect_ok
        self.fail_place = fail_place
        self.wide_spread = wide_spread
        self.low_liquidity = low_liquidity
        self.thin_book = thin_book
        self.place_calls = 0
        self.get_order_calls = 0
        self.cancel_calls = 0
        self.open_order_results: list[OrderResult] = []
        self.order_status_sequence = order_status_sequence or [OrderStatus.FILLED]

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
        elif self.low_liquidity:
            bids = [(Decimal("100"), Decimal("1"))]
            asks = [(Decimal("100.1"), Decimal("1"))]
        elif self.thin_book:
            bids = [(Decimal("100"), Decimal("2"))]
            asks = [(Decimal("100.1"), Decimal("1")), (Decimal("102"), Decimal("1"))]
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
        return list(self.open_order_results)

    async def cancel_order(self, broker_order_id: str):
        self.cancel_calls += 1
        return True

    async def get_positions(self):
        return [
            Position(
                symbol="SPY",
                asset_class=AssetClass.EQUITY,
                quantity=Decimal("2"),
                avg_entry_price=Decimal("100"),
                current_price=Decimal("100.1"),
                unrealised_pnl=Decimal("0"),
                broker="ibkr",
            )
        ]

    async def get_last_price(self, symbol: str) -> Decimal:
        return Decimal("100.25")

    async def get_order(self, broker_order_id: str):
        self.get_order_calls += 1
        idx = min(self.get_order_calls - 1, len(self.order_status_sequence) - 1)
        status = self.order_status_sequence[idx]
        return OrderResult(
            broker_order_id=broker_order_id,
            client_order_id="cid",
            status=status,
            symbol="SPY",
            side=OrderSide.BUY,
            quantity=Decimal("10"),
            filled_quantity=Decimal("5") if status == OrderStatus.PARTIALLY_FILLED else Decimal("10"),
            avg_fill_price=Decimal("100.1"),
            fee=Decimal("0"),
            timestamp=datetime.now(timezone.utc).isoformat(),
        )


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

    engine = ExecutionEngine(broker_configs={}, paper_mode=False)
    result = await engine.execute(_signal(), _approved_decision())
    assert result is None
    assert "ibkr" in risk.disabled


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

    engine = ExecutionEngine(broker_configs={}, paper_mode=False)
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
    assert "ibkr" in risk.disabled


@pytest.mark.asyncio
async def test_reconcile_positions_happy_path(monkeypatch) -> None:
    risk = _FakeRiskEngine(
        {
            "auto_kill_on_reconciliation_failure": True,
            "auto_kill_on_api_failure": False,
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
            return [SimpleNamespace(broker="ibkr", symbol="SPY", quantity=Decimal("2"))]

    class _FakeSession:
        async def execute(self, _stmt):
            return _FakeScalarResult(datetime.now(timezone.utc))

        def add(self, _obj) -> None:
            pass

        async def commit(self) -> None:
            pass

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
    await engine._get_broker("ibkr")
    ok = await engine.reconcile_positions()
    assert ok is True
    assert risk.killed is False


@pytest.mark.asyncio
async def test_retry_exhaustion_returns_none(monkeypatch) -> None:
    risk = _FakeRiskEngine({"auto_kill_on_api_failure": False})
    set_risk_engine(risk)
    broker = _FakeBroker(fail_place=True)
    monkeypatch.setattr("execution.engine.get_broker", lambda *args, **kwargs: broker)
    engine = ExecutionEngine(broker_configs={}, paper_mode=False, place_order_retries=2)
    res = await engine.execute(_signal(), _approved_decision())
    assert res is None
    assert broker.place_calls == 3


@pytest.mark.asyncio
async def test_fill_tracking_timeout_cancels_partial_remainder(monkeypatch) -> None:
    risk = _FakeRiskEngine(
        {
            "max_spread_pct": Decimal("0.05"),
            "min_liquidity_usd": Decimal("1000"),
            "max_slippage_pct": Decimal("0.05"),
            "auto_kill_on_api_failure": False,
        }
    )
    set_risk_engine(risk)
    broker = _FakeBroker(order_status_sequence=[OrderStatus.PARTIALLY_FILLED, OrderStatus.PARTIALLY_FILLED])
    monkeypatch.setattr("execution.engine.get_broker", lambda *args, **kwargs: broker)
    engine = ExecutionEngine(
        broker_configs={},
        paper_mode=True,
        fill_poll_timeout_sec=0.2,
        fill_poll_interval_sec=0.1,
        cancel_partial_on_timeout=True,
    )
    res = await engine.execute(_signal(), _approved_decision())
    assert res is not None
    assert broker.cancel_calls >= 1
    assert broker.get_order_calls >= 2


@pytest.mark.asyncio
async def test_rejects_on_liquidity_and_slippage_limits(monkeypatch) -> None:
    risk = _FakeRiskEngine(
        {
            "max_spread_pct": Decimal("0.05"),
            "min_liquidity_usd": Decimal("1000000"),
            "max_slippage_pct": Decimal("0.005"),
            "auto_kill_on_api_failure": False,
        }
    )
    set_risk_engine(risk)
    low_liq_broker = _FakeBroker(low_liquidity=True)
    monkeypatch.setattr("execution.engine.get_broker", lambda *args, **kwargs: low_liq_broker)
    engine = ExecutionEngine(broker_configs={}, paper_mode=False)
    res1 = await engine.execute(_signal(), _approved_decision())
    assert res1 is None
    assert low_liq_broker.place_calls == 0

    risk2 = _FakeRiskEngine(
        {
            "max_spread_pct": Decimal("0.05"),
            "min_liquidity_usd": Decimal("1"),
            "max_slippage_pct": Decimal("0.005"),
            "auto_kill_on_api_failure": False,
        }
    )
    set_risk_engine(risk2)
    thin_book_broker = _FakeBroker(thin_book=True)
    monkeypatch.setattr("execution.engine.get_broker", lambda *args, **kwargs: thin_book_broker)
    # Larger qty to force walking into worse levels.
    s = _signal()
    s.suggested_quantity = Decimal("2")
    engine2 = ExecutionEngine(broker_configs={}, paper_mode=False)
    res2 = await engine2.execute(s, _approved_decision())
    assert res2 is None
    assert thin_book_broker.place_calls == 0


@pytest.mark.asyncio
async def test_paper_simulate_fill_applies_fee_and_buy_limit_cap() -> None:
    risk = _FakeRiskEngine({"paper_fee_bps": 100})
    set_risk_engine(risk)
    engine = ExecutionEngine(broker_configs={}, paper_mode=True)
    broker = _FakeBroker()
    sig = _signal()
    sig.suggested_price = Decimal("200")
    order = Order(
        symbol="SPY",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Decimal("10"),
        limit_price=Decimal("150"),
        client_order_id="x",
    )
    res = await engine._simulate_fill(order, sig, broker=broker)
    assert res.avg_fill_price == Decimal("150")
    assert res.fee == (Decimal("10") * Decimal("150") * Decimal("100") / Decimal("10000"))


@pytest.mark.asyncio
async def test_paper_simulate_fill_uses_last_price_when_no_suggested() -> None:
    risk = _FakeRiskEngine({"paper_fee_bps": 10})
    set_risk_engine(risk)
    engine = ExecutionEngine(broker_configs={}, paper_mode=True)
    broker = _FakeBroker()
    sig = _signal()
    sig.suggested_price = None
    order = Order(
        symbol="SPY",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=Decimal("2"),
        client_order_id="x",
    )
    res = await engine._simulate_fill(order, sig, broker=broker)
    assert res.avg_fill_price == Decimal("100.25")
    notional = Decimal("2") * Decimal("100.25")
    assert res.fee == (notional * Decimal("10") / Decimal("10000")).quantize(Decimal("0.00000001"))


@pytest.mark.asyncio
async def test_paper_simulate_fill_sell_limit_respects_floor() -> None:
    risk = _FakeRiskEngine({"paper_fee_bps": 10})
    set_risk_engine(risk)
    engine = ExecutionEngine(broker_configs={}, paper_mode=True)
    broker = _FakeBroker()
    sig = _signal()
    sig.side = "sell"
    sig.suggested_price = Decimal("90")
    order = Order(
        symbol="SPY",
        side=OrderSide.SELL,
        order_type=OrderType.LIMIT,
        quantity=Decimal("1"),
        limit_price=Decimal("100"),
        client_order_id="x",
    )
    res = await engine._simulate_fill(order, sig, broker=broker)
    assert res.avg_fill_price == Decimal("100")


@pytest.mark.asyncio
async def test_cancel_all_invokes_cancel_for_each_open_order() -> None:
    now = datetime.now(timezone.utc).isoformat()
    o_a = OrderResult(
        broker_order_id="open-a",
        client_order_id=None,
        status=OrderStatus.OPEN,
        symbol="SPY",
        side=OrderSide.BUY,
        quantity=Decimal("1"),
        filled_quantity=Decimal("0"),
        avg_fill_price=None,
        fee=None,
        timestamp=now,
    )
    o_b = OrderResult(
        broker_order_id="open-b",
        client_order_id=None,
        status=OrderStatus.OPEN,
        symbol="BTC",
        side=OrderSide.SELL,
        quantity=Decimal("1"),
        filled_quantity=Decimal("0"),
        avg_fill_price=None,
        fee=None,
        timestamp=now,
    )
    ibkr = _FakeBroker()
    ibkr.open_order_results = [o_a]
    kraken = _FakeBroker()
    kraken.open_order_results = [o_b]
    engine = ExecutionEngine(broker_configs={}, paper_mode=True)
    engine._brokers = {"ibkr": ibkr, "kraken": kraken}
    await engine.cancel_all()
    assert ibkr.cancel_calls == 1
    assert kraken.cancel_calls == 1

