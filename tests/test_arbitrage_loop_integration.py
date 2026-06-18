from __future__ import annotations

from decimal import Decimal

import pytest

from portfolio.global_edge_coordinator import CoordinatorAction
from risk.engine import RiskDecision, RiskEngine, RiskVerdict
from signals.arb_bridge import process_coordinator_action
from signals.engine import SignalEngine


def _risk_cfg() -> dict:
    return {
        "fundamentals_path": "config/fundamentals.yaml",
        "max_position_pct": 0.10,
        "max_concentration_pct": 0.20,
        "max_gross_exposure_pct": 0.80,
        "max_daily_loss_pct": 0.02,
        "max_drawdown_pct": 0.10,
        "max_crypto_pct": 0.30,
        "max_single_stock_pct": 0.10,
        "max_bond_pct": 0.50,
        "max_consecutive_losses": 3,
        "cooldown_minutes": 60,
        "min_signal_confidence": 0.55,
        "proportionality_threshold_pct": 0.05,
        "minimum_order_sizes_gbp": {
            "crypto": 10,
            "equity": 50,
            "etf": 50,
            "bond": 1000,
            "forex": 1000,
            "future": 5000,
            "option": 500,
        },
    }


def _portfolio_ok() -> dict:
    return {
        "portfolio_value": Decimal("100000"),
        "daily_realized_pnl": Decimal("0"),
        "current_gross_exposure": Decimal("0"),
        "symbol_exposure": {},
        "asset_class_exposure": {},
    }


def test_coordinator_action_to_signal_passes_risk() -> None:
    se_cfg = {
        "default_position_pct": 0.05,
        "quantity_decimals": 4,
        "news_veto_threshold": -0.7,
        "news_confidence_weight": 0.15,
    }
    sig_engine = SignalEngine(se_cfg)
    action = CoordinatorAction(
        kind="open_strategy",
        symbol="SPY",
        strategy_name="momentum_breakout",
        capital=Decimal("5000"),
        priority_score=Decimal("0.4"),
        metadata={
            "side": "long",
            "confidence": 0.9,
            "broker": "ibkr",
            "asset_class": "equity",
            "last_price": "400",
        },
    )
    proc = process_coordinator_action(
        action,
        sig_engine,
        portfolio_value=Decimal("100000"),
        news_score=None,
    )
    assert proc is not None
    assert proc.symbol == "SPY"
    assert proc.suggested_quantity > 0

    engine = RiskEngine(_risk_cfg())
    decision = engine.evaluate(proc, _portfolio_ok())
    assert decision.verdict == RiskVerdict.APPROVED


def test_allocator_selected_open_does_not_run_meta_label_twice(monkeypatch) -> None:
    """Once global-edge selects an open, it should proceed to risk/execution."""
    se_cfg = {
        "default_position_pct": 0.05,
        "quantity_decimals": 4,
        "news_veto_threshold": -0.7,
        "news_confidence_weight": 0.15,
    }
    sig_engine = SignalEngine(se_cfg)
    # D169 — the meta-labeller is now always SCORED (so shadow + the
    # scoreboard see every signal) but only ENFORCES a drop when
    # ``enforce=True``; allocator-selected opens are called with
    # ``enforce=False`` and must NOT be dropped. The stub mirrors the real
    # method's contract: drop only when enforced.
    monkeypatch.setattr(
        sig_engine,
        "_apply_trained_meta_label",
        lambda *a, **k: not k.get("enforce", True),
    )
    action = CoordinatorAction(
        kind="open_strategy",
        symbol="BA",
        strategy_name="event_driven_news",
        capital=Decimal("10840.47"),
        priority_score=Decimal("0.4"),
        metadata={
            "side": "long",
            "confidence": "0.82",
            "broker": "ibkr",
            "asset_class": "equity",
            "last_price": "232",
            "allocation_selected": True,
        },
    )

    proc = process_coordinator_action(
        action,
        sig_engine,
        portfolio_value=Decimal("536450"),
        news_score=None,
    )

    assert proc is not None
    assert proc.symbol == "BA"
    assert proc.confidence == pytest.approx(0.82)


@pytest.mark.asyncio
async def test_smart_order_executor_polls_until_filled() -> None:
    from datetime import datetime, timezone

    from brokers.base import OrderSide, OrderStatus, OrderResult
    from execution.smart_order_executor import SmartOrderExecutor

    class _Leg:
        def __init__(self, name: str) -> None:
            self.name = name
            self.polls = 0

        async def place_order(self, order):
            return OrderResult(
                broker_order_id=f"{self.name}-oid",
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

        async def get_order(self, broker_order_id: str):
            self.polls += 1
            return OrderResult(
                broker_order_id=broker_order_id,
                client_order_id="cid",
                status=OrderStatus.FILLED if self.polls >= 2 else OrderStatus.OPEN,
                symbol="BTCUSDT",
                side=OrderSide.BUY,
                quantity=Decimal("1"),
                filled_quantity=Decimal("1"),
                avg_fill_price=Decimal("100"),
                fee=None,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )

    buy = _Leg("buy")
    sell = _Leg("sell")
    ex = SmartOrderExecutor({"kraken": buy, "binance": sell}, latency_optimizer=type("L", (), {"is_too_slow": lambda self, a, b: False})())
    sig = {
        "buy_venue": "kraken",
        "sell_venue": "binance",
        "symbol": "BTCUSDT",
    }
    out = await ex.execute_spot_arbitrage(sig, Decimal("1"), max_latency_ms=5000.0)
    assert out.get("status") == "submitted"
    assert buy.polls >= 1 and sell.polls >= 1


@pytest.mark.asyncio
async def test_zero_allocation_blocks_stale_open_before_risk() -> None:
    """If the slider drops to zero mid-iteration, stale open actions must not execute."""
    from signals.engine import Signal
    from system.trading_loop.loop import TradingLoop

    class _Router:
        calls = 0

        def route(self, *_args, **_kwargs):
            self.calls += 1
            return "ibkr"

    class _Risk:
        calls = 0

        def update_high_watermark(self, *_args, **_kwargs):
            pass

        def restore_runtime_state(self, *_args, **_kwargs):
            pass

        async def evaluate_and_persist(self, *_args, **_kwargs):
            self.calls += 1
            return RiskDecision(
                verdict=RiskVerdict.APPROVED,
                reason="ok",
                signal_id="s-zero",
                checks_passed=[],
                checks_failed=[],
            )

    class _Execution:
        calls = 0

        async def execute(self, *_args, **_kwargs):
            self.calls += 1
            return None

    loop = TradingLoop(broker_configs={}, available_brokers=["ibkr"], paper_mode=True, capital_pct=0)
    router = _Router()
    risk = _Risk()
    execution = _Execution()
    loop.router = router
    loop.risk_engine = risk
    loop.execution_engine = execution
    signal = Signal(
        signal_id="s-zero",
        symbol="SPY",
        side="buy",
        strategy="global_edge",
        confidence=0.9,
        suggested_quantity=Decimal("1"),
        suggested_price=Decimal("100"),
        broker="ibkr",
        asset_class="equity",
        timestamp="2026-04-28T00:00:00+00:00",
        metadata={"allocation_selected": True},
    )

    ok = await loop._process_signal_global(
        signal,
        session_factory=None,
        portfolio_dict={"portfolio_value": Decimal("100000")},
        total_equity=Decimal("100000"),
        tradable=Decimal("0"),
        sc_log_buffer=[],
    )

    assert ok is False
    assert router.calls == 0
    assert risk.calls == 0
    assert execution.calls == 0


@pytest.mark.asyncio
async def test_paper_reduce_uses_position_broker_even_when_offline(monkeypatch) -> None:
    """Paper flatten closes the ledger broker, not a replacement venue."""
    from signals.engine import Signal
    from system.trading_loop.loop import TradingLoop

    class _Router:
        calls = 0

        def route(self, *_args, **_kwargs):
            self.calls += 1
            return "alpaca"

    class _Risk:
        calls = 0
        broker_seen = None

        def update_high_watermark(self, *_args, **_kwargs):
            pass

        def restore_runtime_state(self, *_args, **_kwargs):
            pass

        async def evaluate_and_persist(self, _session_factory, signal, _portfolio_dict):
            self.calls += 1
            self.broker_seen = signal.broker
            return RiskDecision(
                verdict=RiskVerdict.APPROVED,
                reason="ok",
                signal_id="s-close",
                checks_passed=[],
                checks_failed=[],
            )

    class _Execution:
        calls = 0
        broker_seen = None

        async def execute(self, signal, *_args, **_kwargs):
            self.calls += 1
            self.broker_seen = signal.broker
            return None

    loop = TradingLoop(broker_configs={}, available_brokers=["alpaca"], paper_mode=True, capital_pct=0)
    router = _Router()
    risk = _Risk()
    execution = _Execution()
    loop.router = router
    loop.risk_engine = risk
    loop.execution_engine = execution
    async def _noop_persist(*_args, **_kwargs):
        return None

    monkeypatch.setattr("system.trading_loop.loop._persist_signal", _noop_persist)
    signal = Signal(
        signal_id="s-close",
        symbol="AIP",
        side="sell",
        strategy="global_edge_flatten",
        confidence=1.0,
        suggested_quantity=Decimal("221"),
        suggested_price=Decimal("26.59"),
        broker="ibkr",
        asset_class="equity",
        timestamp="2026-04-28T00:00:00+00:00",
        metadata={"reduce_only": True, "close_only": True, "broker": "ibkr"},
    )

    await loop._process_signal_global(
        signal,
        session_factory=None,
        portfolio_dict={"portfolio_value": Decimal("100000")},
        total_equity=Decimal("100000"),
        tradable=Decimal("0"),
        sc_log_buffer=[],
    )

    assert router.calls == 0
    assert risk.calls == 1
    assert execution.calls == 1
    assert risk.broker_seen == "ibkr"
    assert execution.broker_seen == "ibkr"


@pytest.mark.asyncio
async def test_paper_portfolio_overlay_preserves_local_ledger_positions() -> None:
    from system.trading_loop.loop import _merge_live_broker_positions_into_portfolio_state

    class _EmptyBroker:
        async def get_positions(self):
            return []

    portfolio = {
        "positions": {
            "AIP": {
                "quantity": Decimal("221"),
                "current_price": Decimal("26.59"),
                "avg_entry_price": Decimal("26.59"),
                "asset_class": "equity",
                "broker": "ibkr",
            }
        },
        "current_gross_exposure": Decimal("5876.39"),
    }
    broker_manager = type("BM", (), {"adapters": {"ibkr": _EmptyBroker()}})()

    await _merge_live_broker_positions_into_portfolio_state(
        portfolio,
        broker_manager,
        paper_mode=True,
    )

    assert "AIP" in portfolio["positions"]
    assert portfolio["positions"]["AIP"]["broker"] == "ibkr"


@pytest.mark.asyncio
async def test_arbitrage_executor_polls_until_filled() -> None:
    from datetime import datetime, timezone

    from brokers.base import OrderSide, OrderStatus, OrderResult
    from execution.arbitrage_executor import ArbitrageExecutor

    class _Spot:
        async def place_order(self, order):
            return OrderResult(
                broker_order_id="s-oid",
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

        async def get_order(self, broker_order_id: str):
            self._n = getattr(self, "_n", 0) + 1
            st = OrderStatus.FILLED if self._n >= 2 else OrderStatus.OPEN
            return OrderResult(
                broker_order_id=broker_order_id,
                client_order_id="cid",
                status=st,
                symbol="BTCUSDT",
                side=OrderSide.BUY,
                quantity=Decimal("1"),
                filled_quantity=Decimal("1") if st == OrderStatus.FILLED else Decimal("0"),
                avg_fill_price=Decimal("100"),
                fee=None,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )

    class _Perp(_Spot):
        pass

    spot = _Spot()
    perp = _Perp()
    ex = ArbitrageExecutor({"kraken": spot, "bybit": perp}, flatten_on_failure=True)
    sig = {
        "symbol": "BTCUSDT",
        "metadata": {"spot_venue": "kraken", "perp_venue": "bybit"},
    }
    out = await ex.open_pair(sig, Decimal("1"))
    assert out["status"] == "opened"
