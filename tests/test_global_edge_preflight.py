from __future__ import annotations

from decimal import Decimal
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from risk.engine import RiskPreflightDecision
from portfolio.global_edge_coordinator import CoordinatorAction
from portfolio.d015_replacement_context import ReplacementContext
from system.trading_loop.loop import TradingLoop


class _Router:
    def route(self, asset_class, symbol, metadata=None):
        return "ibkr"


class _Risk:
    def preflight_capacity(self, signal, portfolio):
        return RiskPreflightDecision(
            ok=True,
            reason="preflight_capacity_ok",
            signal_id="s1",
            checks_passed=["single_name_notional"],
            checks_failed=[],
            effective_quantity=Decimal("0.4"),
            effective_notional=Decimal("40"),
        )


class _Execution:
    _wave9_cfg = None

    async def _get_broker(self, name):
        return object()

    def _build_order(self, signal):
        return SimpleNamespace(
            symbol=signal.symbol,
            side=signal.side,
            quantity=Decimal(str(signal.suggested_quantity)),
            limit_price=Decimal(str(signal.suggested_price)),
        )

    async def _apply_marketable_limit(self, order, signal, broker):
        return order

    async def _normalize_order_for_broker(self, order, signal, broker):
        if order.quantity < Decimal("1"):
            order.quantity = Decimal("0")
        return order

    async def _paper_stale_price_precheck(self, order, signal, *, broker=None, session_factory=None):
        return None, {}

    async def _passes_execution_limits(self, broker, order, *, broker_name, allow_auto_kill=True):
        return True


class _LiquidityExecution(_Execution):
    def __init__(self):
        self._last_execution_limit_meta = {}
        self.seen_notional: list[Decimal] = []

    async def _passes_execution_limits(self, broker, order, *, broker_name, allow_auto_kill=True):
        notional = Decimal(str(order.quantity)) * Decimal(str(order.limit_price))
        self.seen_notional.append(notional)
        if len(self.seen_notional) == 1:
            self._last_execution_limit_meta = {
                "execution_limit_reason": "liquidity_limit",
                "book_liquidity_usd": "50",
                "min_liquidity_usd": "100",
                "order_notional": str(notional),
            }
            return False
        self._last_execution_limit_meta = {"execution_limit_reason": "passed"}
        return notional <= Decimal("50")


class _AutoKillExecution(_Execution):
    def __init__(self):
        self._last_execution_limit_meta = {}
        self.auto_kill_allowed: list[bool] = []

    async def _passes_execution_limits(self, broker, order, *, broker_name, allow_auto_kill=True):
        self.auto_kill_allowed.append(bool(allow_auto_kill))
        self._last_execution_limit_meta = {
            "execution_limit_reason": "order_book_fetch_failed",
            "execution_limit_error": "boom",
        }
        return False


@pytest.mark.asyncio
async def test_built_signal_preflight_uses_risk_effective_quantity_for_normalization():
    loop = TradingLoop({}, ["ibkr"], paper_mode=True)
    loop.router = _Router()
    loop.risk_engine = _Risk()
    loop.execution_engine = _Execution()

    signal = SimpleNamespace(
        signal_id="s1",
        symbol="BIL",
        strategy="mean_reversion",
        side="sell",
        confidence=Decimal("0.51"),
        broker="",
        asset_class="equity",
        suggested_price=Decimal("100"),
        suggested_quantity=Decimal("10"),
        metadata={},
    )
    action = SimpleNamespace(kind="open_strategy", priority_score=Decimal("0.1"))

    reason, meta = await loop._preflight_built_signal(
        signal,
        action,
        portfolio_dict={},
        session_factory=None,
    )

    assert reason == "invalid_quantity_after_normalization"
    assert meta["post_normalized_quantity"] == "0"
    assert meta["preflight_capacity_effective_quantity"] == "0.4"


def test_coordinator_action_resize_carries_risk_effective_notional():
    loop = TradingLoop({}, ["ibkr"], paper_mode=True)
    action = CoordinatorAction(
        kind="open_strategy",
        symbol="SPY",
        strategy_name="mean_reversion",
        capital=Decimal("1000"),
        priority_score=Decimal("0.5"),
        metadata={"sizing_cash_factor": "1.0", "sizing_final_capital_required": "1000"},
    )

    resized = loop._coordinator_action_with_effective_capacity(
        action,
        effective_notional=Decimal("250"),
        effective_quantity=Decimal("1.25"),
    )

    assert resized is not action
    assert resized.capital == Decimal("250")
    assert resized.metadata["risk_notional_override"] == "250"
    assert resized.metadata["target_notional"] == "250"
    assert resized.metadata["sizing_final_capital_required"] == "250"
    assert resized.metadata["preflight_capacity_original_action_capital"] == "1000"


def test_liquidity_capacity_resizes_from_execution_metadata():
    loop = TradingLoop({}, ["ibkr"], paper_mode=True)
    action = CoordinatorAction(
        kind="open_strategy",
        symbol="ETH-USD",
        strategy_name="mean_reversion",
        capital=Decimal("100"),
        priority_score=Decimal("0.5"),
        metadata={},
    )

    resized = loop._coordinator_action_with_execution_liquidity_capacity(
        action,
        {
            "execution_limit_reason": "liquidity_limit",
            "post_normalized_quantity": "10",
            "book_liquidity_usd": "50",
            "min_liquidity_usd": "100",
            "order_notional": "100",
        },
    )

    assert resized is not None
    assert resized.capital == Decimal("50")
    assert resized.metadata["execution_liquidity_effective_notional"] == "50.0"


@pytest.mark.asyncio
async def test_built_signal_preflight_resizes_liquidity_reject_and_retries():
    loop = TradingLoop({}, ["ibkr"], paper_mode=True)
    loop.router = _Router()
    loop.risk_engine = None
    loop.execution_engine = _LiquidityExecution()

    signal = SimpleNamespace(
        signal_id="s1",
        symbol="ETH-USD",
        strategy="mean_reversion",
        side="buy",
        confidence=Decimal("0.95"),
        broker="",
        asset_class="crypto",
        suggested_price=Decimal("10"),
        suggested_quantity=Decimal("10"),
        metadata={},
    )
    action = SimpleNamespace(kind="open_strategy", priority_score=Decimal("0.5"), metadata={})

    reason, meta = await loop._preflight_built_signal(
        signal,
        action,
        portfolio_dict={},
        session_factory=None,
    )

    assert reason is None
    assert meta["execution_liquidity_resized"] is True
    assert signal.suggested_quantity == Decimal("5.0")
    assert signal.metadata["risk_notional_override"] == "50.0"


@pytest.mark.asyncio
async def test_preflight_execution_limits_does_not_allow_auto_kill():
    loop = TradingLoop({}, ["ibkr"], paper_mode=True)
    execution = _AutoKillExecution()
    loop.execution_engine = execution

    signal = SimpleNamespace(
        signal_id="s1",
        symbol="SPY",
        strategy="mean_reversion",
        side="buy",
        confidence=Decimal("0.5"),
        broker="ibkr",
        asset_class="equity",
        suggested_price=Decimal("10"),
        suggested_quantity=Decimal("1"),
        metadata={},
    )

    reason, meta = await loop._preflight_execution_limits(signal)

    assert reason == "execution_precheck_rejected"
    assert meta["execution_limit_reason"] == "order_book_fetch_failed"
    assert execution.auto_kill_allowed == [False]


@pytest.mark.asyncio
async def test_orchestrator_reserve_uses_action_budget(monkeypatch):
    loop = TradingLoop({}, ["ibkr"], paper_mode=True)
    loop.sig_engine = object()
    loop._global_edge_cfg = {"max_actions_per_tick": {"hunter": 2, "trader": 1}}
    processed: list[str] = []

    async def _resolve_price(_symbol):
        return Decimal("10")

    async def _preflight(signal, action, *, portfolio_dict, session_factory):
        return None, {}

    async def _process(signal, session_factory, portfolio_dict, total_equity, tradable, sc_log_buffer=None):
        processed.append(signal.symbol)
        return True

    def _build_signal(action, sig_engine, *, portfolio_value, news_score):
        return SimpleNamespace(
            signal_id=f"s-{action.symbol}",
            symbol=action.symbol,
            side="buy",
            strategy=action.strategy_name,
            confidence=Decimal("0.9"),
            suggested_quantity=Decimal("1"),
            suggested_price=Decimal("10"),
            asset_class=(action.metadata or {}).get("asset_class", "crypto"),
            metadata=dict(action.metadata or {}),
        )

    monkeypatch.setattr(loop, "_preflight_built_signal", _preflight)
    monkeypatch.setattr(loop, "_process_signal_global", _process)
    monkeypatch.setattr("system.trading_loop.loop.process_coordinator_action", _build_signal)

    candidates = [
        SimpleNamespace(
            symbol="ETH-USD",
            side="long",
            strategy_name="mean_reversion",
            asset_class="crypto",
            confidence=Decimal("0.95"),
            metadata={"target_notional": "100"},
        ),
        SimpleNamespace(
            symbol="BTC-USD",
            side="long",
            strategy_name="mean_reversion",
            asset_class="crypto",
            confidence=Decimal("0.90"),
            metadata={"target_notional": "100"},
        ),
        SimpleNamespace(
            symbol="XRP-USD",
            side="long",
            strategy_name="mean_reversion",
            asset_class="crypto",
            confidence=Decimal("0.85"),
            metadata={"target_notional": "100"},
        ),
    ]

    executed = await loop._run_orchestrator_reserve_candidates(
        batch_candidates=candidates,
        attempted_symbols=set(),
        portfolio_dict={},
        tradable=Decimal("100000"),
        total_equity=Decimal("100000"),
        session_factory=None,
        resolve_price=_resolve_price,
        mode_raw="hunter",
    )

    assert executed == 2
    assert processed == ["ETH-USD", "BTC-USD"]


@pytest.mark.asyncio
async def test_orchestrator_reserve_nets_admitted_target_against_existing_book(monkeypatch):
    loop = TradingLoop({}, ["ibkr"], paper_mode=True)
    loop.sig_engine = object()
    loop._global_edge_cfg = {"max_actions_per_tick": {"hunter": 3}}
    processed: list[tuple[str, Decimal, Decimal]] = []

    async def _resolve_price(_symbol):
        return Decimal("10")

    async def _preflight(signal, action, *, portfolio_dict, session_factory):
        processed.append((signal.symbol, action.capital, signal.suggested_quantity))
        return None, {}

    async def _process(signal, session_factory, portfolio_dict, total_equity, tradable, sc_log_buffer=None):
        return True

    def _build_signal(action, sig_engine, *, portfolio_value, news_score):
        return SimpleNamespace(
            signal_id=f"s-{action.symbol}",
            symbol=action.symbol,
            side="buy",
            strategy=action.strategy_name,
            confidence=Decimal("0.9"),
            suggested_quantity=action.capital / Decimal("10"),
            suggested_price=Decimal("10"),
            asset_class="crypto",
            metadata=dict(action.metadata or {}),
        )

    monkeypatch.setattr(loop, "_preflight_built_signal", _preflight)
    monkeypatch.setattr(loop, "_process_signal_global", _process)
    monkeypatch.setattr("system.trading_loop.loop.process_coordinator_action", _build_signal)

    candidates = [
        SimpleNamespace(
            symbol="ETH-USD",
            side="long",
            strategy_name="mean_reversion",
            asset_class="crypto",
            confidence=Decimal("0.95"),
            metadata={"target_notional": "100"},
        ),
        SimpleNamespace(
            symbol="BTC-USD",
            side="long",
            strategy_name="mean_reversion",
            asset_class="crypto",
            confidence=Decimal("0.90"),
            metadata={"target_notional": "100"},
        ),
    ]
    portfolio = {
        "positions": {
            "kraken:ETH-USD": {
                "symbol": "ETH-USD",
                "quantity": Decimal("12"),
                "current_price": Decimal("10"),
            },
            "binance:BTC-USD": {
                "symbol": "BTC-USD",
                "quantity": Decimal("4"),
                "current_price": Decimal("10"),
            },
        },
        "symbol_exposure": {
            "ETH-USD": Decimal("120"),
            "BTC-USD": Decimal("40"),
        },
    }

    executed = await loop._run_orchestrator_reserve_candidates(
        batch_candidates=candidates,
        attempted_symbols=set(),
        portfolio_dict=portfolio,
        tradable=Decimal("100000"),
        total_equity=Decimal("100000"),
        session_factory=None,
        resolve_price=_resolve_price,
        mode_raw="hunter",
    )

    assert executed == 1
    assert processed == [("BTC-USD", Decimal("60"), Decimal("6"))]


@pytest.mark.asyncio
async def test_orchestrator_reserve_defers_recently_recycled_symbol(monkeypatch):
    loop = TradingLoop({}, ["ibkr"], paper_mode=True)
    loop.sig_engine = object()
    loop._global_edge_cfg = {
        "max_actions_per_tick": {"hunter": 1},
        "capital_recycle": {"symbol_cooldown_sec": 900},
    }
    processed: list[str] = []

    async def _resolve_price(_symbol):
        return Decimal("10")

    async def _process(signal, session_factory, portfolio_dict, total_equity, tradable, sc_log_buffer=None):
        processed.append(signal.symbol)
        return True

    monkeypatch.setattr(loop, "_process_signal_global", _process)
    context = ReplacementContext(
        last_cull_at_by_symbol={"ETH": datetime.now(timezone.utc)}
    )
    candidate = SimpleNamespace(
        symbol="ETH-USD",
        side="long",
        strategy_name="mean_reversion",
        asset_class="crypto",
        confidence=Decimal("0.95"),
        metadata={"target_notional": "100"},
    )

    executed = await loop._run_orchestrator_reserve_candidates(
        batch_candidates=[candidate],
        attempted_symbols=set(),
        portfolio_dict={},
        tradable=Decimal("100000"),
        total_equity=Decimal("100000"),
        session_factory=None,
        resolve_price=_resolve_price,
        mode_raw="hunter",
        replacement_context=context,
    )

    assert executed == 0
    assert processed == []


@pytest.mark.asyncio
async def test_orchestrator_reserve_applies_outcome_learning_to_absolute_target(monkeypatch):
    loop = TradingLoop({}, ["ibkr"], paper_mode=True)
    loop.sig_engine = object()
    loop._global_edge_cfg = {"max_actions_per_tick": {"hunter": 1}}
    monkeypatch.setattr(
        loop.trade_admission,
        "outcome_size_multiplier",
        lambda **_kwargs: Decimal("0.2"),
    )
    processed: list[str] = []

    async def _resolve_price(_symbol):
        return Decimal("10")

    async def _process(signal, session_factory, portfolio_dict, total_equity, tradable, sc_log_buffer=None):
        processed.append(signal.symbol)
        return True

    def _build_signal(action, sig_engine, *, portfolio_value, news_score):
        return SimpleNamespace(
            signal_id="s-eth",
            symbol=action.symbol,
            side="buy",
            strategy=action.strategy_name,
            confidence=Decimal("0.9"),
            suggested_quantity=Decimal("10"),
            suggested_price=Decimal("10"),
            asset_class="crypto",
            metadata=dict(action.metadata or {}),
        )

    monkeypatch.setattr(loop, "_process_signal_global", _process)
    monkeypatch.setattr("system.trading_loop.loop.process_coordinator_action", _build_signal)
    candidate = SimpleNamespace(
        symbol="ETH-USD",
        side="long",
        strategy_name="mean_reversion",
        asset_class="crypto",
        confidence=Decimal("0.95"),
        metadata={"target_notional": "100"},
    )
    portfolio = {
        "positions": {
            "kraken:ETH-USD": {
                "symbol": "ETH-USD",
                "quantity": Decimal("4"),
                "current_price": Decimal("10"),
            }
        },
        "symbol_exposure": {"ETH-USD": Decimal("40")},
    }

    executed = await loop._run_orchestrator_reserve_candidates(
        batch_candidates=[candidate],
        attempted_symbols=set(),
        portfolio_dict=portfolio,
        tradable=Decimal("100000"),
        total_equity=Decimal("100000"),
        session_factory=None,
        resolve_price=_resolve_price,
        mode_raw="hunter",
    )

    assert executed == 0
    assert processed == []
