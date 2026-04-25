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
        place_status: OrderStatus = OrderStatus.OPEN,
        order_status_sequence: list[OrderStatus] | None = None,
    ):
        self.broker_name = "ibkr"
        self.connect_ok = connect_ok
        self.fail_place = fail_place
        self.wide_spread = wide_spread
        self.low_liquidity = low_liquidity
        self.thin_book = thin_book
        self.place_status = place_status
        self.place_calls = 0
        self.last_order = None
        self.get_order_calls = 0
        self.cancel_calls = 0
        self.open_order_results: list[OrderResult] = []
        self.order_status_sequence = order_status_sequence or [OrderStatus.FILLED]

    async def connect(self) -> bool:
        return self.connect_ok

    async def is_connected(self) -> bool:
        return self.connect_ok

    async def place_order(self, order):
        self.place_calls += 1
        self.last_order = order
        if self.fail_place:
            raise RuntimeError("broker down")
        return OrderResult(
            broker_order_id="b-1",
            client_order_id=order.client_order_id,
            status=self.place_status,
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
async def test_rejected_order_without_broker_reason_gets_persistable_reason(monkeypatch) -> None:
    risk = _FakeRiskEngine(
        {
            "max_spread_pct": Decimal("0.05"),
            "min_liquidity_usd": Decimal("1000"),
            "max_slippage_pct": Decimal("0.05"),
            "auto_kill_on_api_failure": False,
        }
    )
    set_risk_engine(risk)
    broker = _FakeBroker(place_status=OrderStatus.REJECTED, order_status_sequence=[OrderStatus.REJECTED])
    monkeypatch.setattr("execution.engine.get_broker", lambda *args, **kwargs: broker)

    engine = ExecutionEngine(broker_configs={}, paper_mode=False)
    result = await engine.execute(_signal(), _approved_decision())

    assert result is not None
    assert result.status == OrderStatus.REJECTED
    assert broker.last_order.instrument_metadata["reject_reason"] == "broker_rejected_without_reason"
    assert broker.last_order.instrument_metadata["error_message"]


@pytest.mark.asyncio
async def test_ibkr_equity_quantity_is_normalized_to_whole_units(monkeypatch) -> None:
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
    s = _signal()
    s.suggested_quantity = Decimal("10.999")
    s.broker = "ibkr"
    s.asset_class = "equity"

    engine = ExecutionEngine(broker_configs={}, paper_mode=True)
    result = await engine.execute(s, _approved_decision())
    assert result is not None
    assert broker.last_order is not None
    assert broker.last_order.quantity == Decimal("10")


@pytest.mark.asyncio
async def test_ibkr_equity_fractional_under_one_is_skipped(monkeypatch) -> None:
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
    s = _signal()
    s.suggested_quantity = Decimal("0.75")
    s.broker = "ibkr"
    s.asset_class = "equity"

    engine = ExecutionEngine(broker_configs={}, paper_mode=False)
    result = await engine.execute(s, _approved_decision())
    assert result is None
    assert broker.place_calls == 0


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

    added_rows: list = []
    commits: list[int] = []

    class _FakeSession:
        async def execute(self, _stmt):
            # max timestamp exists, but no local rows -> mismatch vs remote qty=2
            return _FakeScalarResult(datetime.now(timezone.utc))

        def add(self, obj) -> None:
            added_rows.append(obj)

        async def commit(self) -> None:
            commits.append(1)

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
    # D030: broker-truth snapshot must still be persisted so the DB / allocator
    # reflect reality even while the mismatch is flagged.
    assert len(added_rows) == 1, "remote snapshot must be persisted on mismatch"
    assert len(commits) == 1, "commit must fire on mismatch persistence"


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
async def test_reconcile_persists_broker_truth_on_quantity_mismatch(monkeypatch) -> None:
    """D030: the broker is ground truth for positions.

    Prior to the fix, a quantity mismatch (e.g. local DB says qty=164 while
    IBKR reports 335) caused an early-return that *kept the stale local
    snapshot*, so the allocator's ``held`` input silently diverged from
    reality. The fix makes persistence unconditional — the DB always reflects
    the broker's books, while ``return False`` still signals the mismatch to
    upstream callers (and the optional auto-kill hook still fires).
    """
    risk = _FakeRiskEngine(
        {
            "auto_kill_on_reconciliation_failure": False,  # opt-in — default off
            "auto_kill_on_api_failure": False,
        }
    )
    set_risk_engine(risk)
    broker = _FakeBroker()  # returns SPY qty=2 (see get_positions fixture)
    monkeypatch.setattr("execution.engine.get_broker", lambda *args, **kwargs: broker)

    class _FakeScalarResult:
        def __init__(self, value):
            self._value = value

        def scalar_one_or_none(self):
            return self._value

        def scalars(self):
            return self

        def all(self):
            # Local DB claims we only hold qty=1 — diverges from broker's qty=2.
            return [SimpleNamespace(broker="ibkr", symbol="SPY", quantity=Decimal("1"))]

    added_rows: list = []
    commits: list[int] = []

    class _FakeSession:
        async def execute(self, _stmt):
            return _FakeScalarResult(datetime.now(timezone.utc))

        def add(self, obj) -> None:
            added_rows.append(obj)

        async def commit(self) -> None:
            commits.append(1)

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

    # Still signals mismatch to upstream.
    assert ok is False
    # Auto-kill is OPT-IN and disabled here → broker stays enabled.
    assert "ibkr" not in risk.disabled
    assert risk.killed is False
    # Critical: broker-truth persisted so the next allocator tick sees qty=2.
    assert len(added_rows) == 1, "remote snapshot must be persisted on mismatch"
    persisted = added_rows[0]
    assert persisted.symbol == "SPY"
    assert persisted.broker == "ibkr"
    assert persisted.quantity == Decimal("2"), "persisted qty must match broker truth"
    assert len(commits) == 1


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
    mid = Decimal("100.25")
    expected_fill = mid * (Decimal("1") + Decimal("2") / Decimal("10000"))  # default paper_slippage_bps
    assert res.avg_fill_price == expected_fill
    notional = Decimal("2") * expected_fill
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


def test_add_allowed_broker_appends_once() -> None:
    from control.runtime import set_execution_engine

    try:
        eng = ExecutionEngine(broker_configs={}, paper_mode=True, allowed_brokers=["kraken"])
        eng.add_allowed_broker("ibkr")
        eng.add_allowed_broker("ibkr")
        assert eng.allowed_brokers == ["kraken", "ibkr"]
    finally:
        set_execution_engine(None)


@pytest.mark.asyncio
async def test_get_broker_uses_broker_manager_adapter(monkeypatch) -> None:
    from control.runtime import set_execution_engine

    def _boom(*_a, **_k):
        raise AssertionError("get_broker should not run when broker_manager supplies adapter")

    monkeypatch.setattr("execution.engine.get_broker", _boom)
    fake = _FakeBroker(connect_ok=True)
    bm = SimpleNamespace(adapters={"ibkr": fake})
    eng = ExecutionEngine(
        broker_configs={"ibkr": {}},
        paper_mode=True,
        allowed_brokers=["ibkr"],
        broker_manager=bm,
    )
    try:
        b = await eng._get_broker("IBKR")
        assert b is fake
        assert eng._brokers["ibkr"] is fake
    finally:
        set_execution_engine(None)


# =============================================================================
# D031C — execution-boundary sizing guard
# =============================================================================


def _sizing_order(symbol: str, qty: Decimal, limit_px: Decimal) -> Order:
    return Order(
        symbol=symbol,
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=qty,
        limit_price=limit_px,
        client_order_id="x-1",
    )


def _sizing_signal(md: dict) -> Signal:
    return Signal(
        signal_id="s-1",
        symbol="COHR",
        side="buy",
        strategy="momentum_breakout",
        confidence=0.8,
        suggested_quantity=Decimal("22"),
        suggested_price=Decimal("355"),
        broker="ibkr",
        asset_class="equity",
        timestamp=datetime.now(timezone.utc).isoformat(),
        metadata=md,
    )


def test_d031c_boundary_guard_allows_order_within_tolerance() -> None:
    """Order notional ≤ 1.25× intended is accepted."""
    risk = _FakeRiskEngine({})
    set_risk_engine(risk)
    eng = ExecutionEngine(broker_configs={}, paper_mode=True)
    # intended = 7913, actual = 22 × 355 = 7810 → ratio 0.987, OK
    order = _sizing_order("COHR", Decimal("22"), Decimal("355"))
    sig = _sizing_signal({
        "sizing_final_capital_required": "7913.22",
        "sizing_source": "target_notional",
    })
    assert eng._passes_sizing_boundary_guard(order, sig) is True


def test_d031c_boundary_guard_rejects_gross_over_sizing() -> None:
    """Order notional >> 1.25× intended is rejected loudly."""
    risk = _FakeRiskEngine({})
    set_risk_engine(risk)
    eng = ExecutionEngine(broker_configs={}, paper_mode=True)
    # intended = 7913, actual = 164 × 355 = 58,220 → ratio 7.35, REJECT
    order = _sizing_order("COHR", Decimal("164"), Decimal("355"))
    sig = _sizing_signal({
        "sizing_final_capital_required": "7913.22",
        "sizing_source": "target_notional",
    })
    assert eng._passes_sizing_boundary_guard(order, sig) is False


def test_d031c_boundary_guard_rejects_over_hard_cap() -> None:
    """Exceeding the hard cap is always rejected even without intended size."""
    risk = _FakeRiskEngine({})
    set_risk_engine(risk)
    eng = ExecutionEngine(broker_configs={}, paper_mode=True)
    order = _sizing_order("COHR", Decimal("1000"), Decimal("355"))  # 355k
    sig = _sizing_signal({
        "sizing_hard_cap_notional": "100000",  # 100k cap
    })
    assert eng._passes_sizing_boundary_guard(order, sig) is False


def test_d031c_boundary_guard_absolute_cap_when_no_sizing_metadata(monkeypatch) -> None:
    """Without sizing audit metadata the guard falls back to an absolute cap.

    The pre-fix guard was a no-op for legacy signals — that's what allowed
    $130M-notional orders to slip through to Alpaca. With the fix, missing
    audit metadata triggers ``EXECUTION_MAX_ORDER_NOTIONAL_USD`` (default
    50,000) as a hard backstop.
    """
    risk = _FakeRiskEngine({})
    set_risk_engine(risk)
    eng = ExecutionEngine(broker_configs={}, paper_mode=True)
    # 10,000 × $355 = $3.55M — well above the 50,000 absolute cap → REJECT.
    order = _sizing_order("COHR", Decimal("10000"), Decimal("355"))
    sig = _sizing_signal({})
    assert eng._passes_sizing_boundary_guard(order, sig) is False

    # A small order with no audit metadata (e.g. legacy strategy) is still
    # allowed when it stays under the absolute cap.
    small_order = _sizing_order("COHR", Decimal("10"), Decimal("355"))  # 3,550
    assert eng._passes_sizing_boundary_guard(small_order, sig) is True


def test_d031c_boundary_guard_absolute_cap_skipped_for_reduce_only() -> None:
    """Reduce-only / stop-loss closes are exempt from the absolute cap."""
    risk = _FakeRiskEngine({})
    set_risk_engine(risk)
    eng = ExecutionEngine(broker_configs={}, paper_mode=True)
    order = _sizing_order("COHR", Decimal("10000"), Decimal("355"))
    sig = _sizing_signal({"reduce_only": True})
    assert eng._passes_sizing_boundary_guard(order, sig) is True


def test_d031c_boundary_guard_exempts_arbitrage() -> None:
    """Arbitrage sides are exempt (capital flows via different path)."""
    risk = _FakeRiskEngine({})
    set_risk_engine(risk)
    eng = ExecutionEngine(broker_configs={}, paper_mode=True)
    order = _sizing_order("BTC-USDT", Decimal("5"), Decimal("70000"))
    sig = _sizing_signal({
        "sizing_final_capital_required": "1000",  # would fail if not arb
    })
    sig.side = "ARBITRAGE_SPOT_SPREAD"
    assert eng._passes_sizing_boundary_guard(order, sig) is True


# =============================================================================
# Telegram notifications — only open/close events (post-D031 operator prefs)
# =============================================================================


@pytest.mark.asyncio
async def test_maybe_notify_fill_sends_for_filled_orders(monkeypatch) -> None:
    """A FILLED OrderResult triggers a Telegram message via _send_critical_alert."""
    risk = _FakeRiskEngine({})
    set_risk_engine(risk)
    eng = ExecutionEngine(broker_configs={}, paper_mode=True)

    captured: list[str] = []

    async def _fake_alert(msg: str) -> None:
        captured.append(msg)

    monkeypatch.setattr(eng, "_send_critical_alert", _fake_alert)

    order = Order(
        symbol="COHR",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Decimal("22"),
        limit_price=Decimal("355"),
        client_order_id="x-fill-1",
    )
    result = OrderResult(
        broker_order_id="bid-1",
        client_order_id="x-fill-1",
        status=OrderStatus.FILLED,
        symbol="COHR",
        side=OrderSide.BUY,
        quantity=Decimal("22"),
        filled_quantity=Decimal("22"),
        avg_fill_price=Decimal("355.10"),
        fee=Decimal("0"),
        timestamp=datetime.now(timezone.utc).isoformat(),
    )
    signal = _sizing_signal({"coordinator_kind": "open_strategy"})
    await eng._maybe_notify_fill(order, result, signal)

    assert len(captured) == 1
    msg = captured[0]
    assert "OPEN" in msg
    assert "FILLED" in msg
    assert "COHR" in msg
    assert "BUY" in msg


@pytest.mark.asyncio
async def test_maybe_notify_fill_labels_close_on_reduce_only(monkeypatch) -> None:
    risk = _FakeRiskEngine({})
    set_risk_engine(risk)
    eng = ExecutionEngine(broker_configs={}, paper_mode=True)

    captured: list[str] = []

    async def _fake_alert(msg: str) -> None:
        captured.append(msg)

    monkeypatch.setattr(eng, "_send_critical_alert", _fake_alert)

    order = Order(
        symbol="COHR",
        side=OrderSide.SELL,
        order_type=OrderType.LIMIT,
        quantity=Decimal("22"),
        limit_price=Decimal("360"),
        client_order_id="x-close-1",
    )
    result = OrderResult(
        broker_order_id="bid-2",
        client_order_id="x-close-1",
        status=OrderStatus.FILLED,
        symbol="COHR",
        side=OrderSide.SELL,
        quantity=Decimal("22"),
        filled_quantity=Decimal("22"),
        avg_fill_price=Decimal("360.00"),
        fee=Decimal("0"),
        timestamp=datetime.now(timezone.utc).isoformat(),
    )
    signal = _sizing_signal({"reduce_only": True})
    signal.side = "sell"
    await eng._maybe_notify_fill(order, result, signal)
    assert len(captured) == 1
    assert "CLOSE" in captured[0]


@pytest.mark.asyncio
async def test_maybe_notify_fill_skips_non_filled_statuses(monkeypatch) -> None:
    risk = _FakeRiskEngine({})
    set_risk_engine(risk)
    eng = ExecutionEngine(broker_configs={}, paper_mode=True)

    captured: list[str] = []

    async def _fake_alert(msg: str) -> None:
        captured.append(msg)

    monkeypatch.setattr(eng, "_send_critical_alert", _fake_alert)

    order = Order(
        symbol="COHR",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Decimal("22"),
        limit_price=Decimal("355"),
        client_order_id="x-pending-1",
    )
    for status in (OrderStatus.PENDING, OrderStatus.OPEN, OrderStatus.CANCELLED, OrderStatus.REJECTED):
        result = OrderResult(
            broker_order_id="bid-3",
            client_order_id="x-pending-1",
            status=status,
            symbol="COHR",
            side=OrderSide.BUY,
            quantity=Decimal("22"),
            filled_quantity=Decimal("0"),
            avg_fill_price=None,
            fee=Decimal("0"),
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
        signal = _sizing_signal({})
        await eng._maybe_notify_fill(order, result, signal)

    assert captured == []


@pytest.mark.asyncio
async def test_sizing_guard_reject_no_longer_sends_telegram(monkeypatch) -> None:
    """The sizing boundary guard must NOT spam Telegram on reject.

    Regression: the April 2026 Telegram flood (100+ "Sizing boundary guard
    rejected signal" messages). Operators now watch the CRITICAL log instead.
    """
    risk = _FakeRiskEngine({})
    set_risk_engine(risk)
    eng = ExecutionEngine(broker_configs={}, paper_mode=True)

    captured: list[str] = []

    async def _fake_alert(msg: str) -> None:
        captured.append(msg)

    monkeypatch.setattr(eng, "_send_critical_alert", _fake_alert)

    order = _sizing_order("COHR", Decimal("164"), Decimal("355"))  # 7.35x intended
    sig = _sizing_signal({
        "sizing_final_capital_required": "7913.22",
        "sizing_source": "target_notional",
    })
    # Guard would reject ...
    assert eng._passes_sizing_boundary_guard(order, sig) is False
    # ... but must not have called alert itself. (execute() used to call it
    # right after this returned False; that code was removed.)
    assert captured == []
