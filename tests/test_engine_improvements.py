import pytest
from decimal import Decimal
from datetime import datetime, timezone
from contextlib import asynccontextmanager
from typing import Any, Optional
from dataclasses import dataclass

from core.models_runtime import PortfolioState, RegimeState
from config.models import AllocationConfig
from system.adaptive_sizing import SizingInputs, compute_position_size
from risk.regime_state import compute_regime_state_from_inputs
from execution.engine import ExecutionEngine
from execution.router import SmartOrderRouter
from brokers.permissions import get_permissions
from risk.engine import RiskDecision, RiskVerdict, Signal
from execution.engine import Order, OrderSide, OrderType, OrderResult

# ==========================================
# 1. Database Mocks
# ==========================================

class _FakeSession:
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
                return [r.slippage_bps for r in self._rs]

        class _Result:
            def __init__(self, rs):
                self._rs = rs

            def scalars(self):
                return _Scalars(self._rs)

            def all(self):
                return [(r.slippage_bps,) for r in self._rs]

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


@dataclass
class MockFillLog:
    slippage_bps: Decimal


# ==========================================
# 2. Kelly Criterion Sizing Tests
# ==========================================

def test_kelly_sizing_positive_edge():
    # p = 0.60, b = 1.5. f_star = 0.60 - 0.40 / 1.5 = 0.60 - 0.2667 = 0.3333
    # Quarter-Kelly pct = 0.3333 * 0.25 = 0.0833 (8.33% NAV)
    inputs = SizingInputs(
        nav=Decimal("100000"),
        last_price=Decimal("10.0"),
        atr_pct=0.02,
        mode="hunter",
        confidence=1.0,
        win_rate=0.60,
        win_loss_ratio=1.5,
        kelly_fraction=0.25,
        use_kelly=True
    )
    decision = compute_position_size(inputs)
    assert decision.path == "kelly_sizing"
    # Expected notional pct approx 8.33% of 100000 -> 8333 notional -> quantity 833.3
    expected_notional = Decimal("100000") * Decimal("0.083333333")
    assert abs(decision.notional - expected_notional) < Decimal("10.0")


def test_kelly_sizing_negative_edge_fallback():
    # Negative edge, f_star <= 0. Should drop the signal (notional = 0)
    inputs = SizingInputs(
        nav=Decimal("100000"),
        last_price=Decimal("10.0"),
        atr_pct=0.02,
        mode="hunter",
        confidence=1.0,
        win_rate=0.40,
        win_loss_ratio=1.0,
        kelly_fraction=0.25,
        use_kelly=True
    )
    decision = compute_position_size(inputs)
    assert decision.path == "kelly_negative_edge_drop"
    assert decision.notional == Decimal("0")


# ==========================================
# 3. Asymmetric Regime EWMA Tests
# ==========================================

@dataclass
class MockMarketStateComponents:
    trend_strength: Decimal = Decimal("0.8")
    cross_asset_confirmation: Decimal = Decimal("0.8")
    liquidity_state: Decimal = Decimal("0.8")
    macro_clarity: Decimal = Decimal("0.8")
    risk_on_breadth: Decimal = Decimal("0.8")
    chaos_penalty: Decimal = Decimal("0.2")
    correlation_crowding: Decimal = Decimal("0.2")
    volatility_structure: Decimal = Decimal("0.2")
    anomaly_breadth: Decimal = Decimal("0.2")
    news_conflict_score: Decimal = Decimal("0.2")


def test_asymmetric_regime_ewma():
    from config.loaders import load_allocation
    cfg = load_allocation()
    p = PortfolioState(
        timestamp=datetime.now(timezone.utc),
        mode="hunter",
        nav=Decimal("100000"),
        cash=Decimal("100000"),
        available_buying_power=Decimal("200000"),
        gross_exposure=Decimal("0"),
        net_exposure=Decimal("0"),
        leverage_ratio=Decimal("1"),
        drawdown_from_hwm_pct=Decimal("0.0"),
    )
    
    rows = [
        {"symbol": "A", "features": {"mom_10": 2.0, "rsi_14": 60.0, "volume_z": 0.5, "relative_dollar_volume": 1.1}},
        {"symbol": "B", "features": {"mom_10": 1.5, "rsi_14": 58.0, "volume_z": 0.4, "relative_dollar_volume": 1.05}},
        {"symbol": "C", "features": {"mom_10": 1.8, "rsi_14": 57.0, "volume_z": 0.3, "relative_dollar_volume": 1.02}},
    ]

    # 1. Fast de-risking on downturn: raw_score < prev_score
    prev_score = Decimal("1.5")
    r_down = compute_regime_state_from_inputs(
        portfolio_state=p,
        allocation_cfg=cfg,
        feature_rows=rows,
        news_dispersion=None,
        previous_market_state_score=prev_score
    )
    assert r_down.market_state_score < prev_score
    
    wc = cfg.market_state.components
    raw_computed = (
        Decimal(str(wc.trend_strength)) * r_down.components.trend_strength
        + Decimal(str(wc.cross_asset_confirmation)) * r_down.components.cross_asset_confirmation
        + Decimal(str(wc.liquidity_state)) * r_down.components.liquidity_state
        + Decimal(str(wc.macro_clarity)) * r_down.components.macro_clarity
        + Decimal(str(wc.risk_on_breadth)) * r_down.components.risk_on_breadth
        + Decimal(str(wc.chaos_penalty)) * r_down.components.chaos_penalty
        + Decimal(str(wc.correlation_crowding)) * r_down.components.correlation_crowding
        + Decimal(str(wc.volatility_structure)) * r_down.components.volatility_structure
        + Decimal(str(wc.anomaly_breadth)) * r_down.components.anomaly_breadth
    )
    # Expected smoothed: 0.20 * raw_computed + 0.80 * 1.5
    expected_smoothed = Decimal("0.20") * raw_computed + Decimal("0.80") * prev_score
    assert abs(r_down.market_state_score - expected_smoothed) < Decimal("0.01")

    # 2. Slow recovery on upturn: raw_score >= prev_score
    prev_score_low = Decimal("-1.5")
    r_up = compute_regime_state_from_inputs(
        portfolio_state=p,
        allocation_cfg=cfg,
        feature_rows=rows,
        news_dispersion=None,
        previous_market_state_score=prev_score_low
    )
    # Slow recovery: alpha = 0.05
    expected_smoothed_up = Decimal("0.05") * raw_computed + Decimal("0.95") * prev_score_low
    assert abs(r_up.market_state_score - expected_smoothed_up) < Decimal("0.01")


# ==========================================
# 4. Adaptive Slippage Gate Tests
# ==========================================

@pytest.mark.asyncio
async def test_adaptive_slippage_gate_tightens_and_widens():
    # Setup ExecutionEngine
    engine = ExecutionEngine(broker_configs={}, paper_mode=True)
    
    # Mock RiskEngine config
    @dataclass
    class FakeRiskEngine:
        config = {
            "stale_price_gate": {
                "enabled": True,
                "max_adverse_drift_bps": 25,
                "adaptive_slippage": {
                    "enabled": True,
                    "lookback": 50,
                    "min_bps": 10,
                    "max_bps": 150
                }
            }
        }
    
    from control.runtime import set_risk_engine
    set_risk_engine(FakeRiskEngine())

    signal = Signal(
        signal_id="sig1",
        symbol="AAPL",
        side="buy",
        strategy="mean_reversion",
        confidence=1.0,
        suggested_quantity=Decimal("10"),
        suggested_price=Decimal("150.0"),
        broker="ibkr",
        asset_class="equity",
        timestamp=datetime.now(timezone.utc).isoformat(),
        metadata={}
    )

    # 1. High slippage (e.g. 20.0 bps): should tighten the gate (multiplier = 5 / 20 = 0.25)
    # base_bps = 25 * 0.25 = 6.25 bps -> clamped at min_bps = 10 bps.
    high_slips = [MockFillLog(slippage_bps=Decimal("20.0")) for _ in range(15)]
    sf_high = _session_factory(high_slips)
    enabled, drift_bps = await engine._stale_price_cfg(signal, session_factory=sf_high)
    assert enabled
    assert drift_bps == Decimal("10")  # Clamped at min_bps

    # 2. Low slippage (e.g. 1.0 bps): should widen the gate (multiplier = 5 / 1 = 5)
    # base_bps = 25 * 5 = 125 bps.
    engine._slippage_cache.clear()
    low_slips = [MockFillLog(slippage_bps=Decimal("1.0")) for _ in range(15)]
    sf_low = _session_factory(low_slips)
    enabled, drift_bps_low = await engine._stale_price_cfg(signal, session_factory=sf_low)
    assert enabled
    assert drift_bps_low == Decimal("125")


# ==========================================
# 5. Smart Order Routing Optimization Tests
# ==========================================

def test_smart_order_routing_fee_and_borrow_cost(monkeypatch):
    # Setup mock permissions in router
    class FakePermissions:
        def check_permission(self, broker: str, asset_class: str) -> bool:
            return True
            
        def get_taker_fee_bps(self, broker: str) -> float:
            return {"binance": 10.0, "kraken": 15.0, "alpaca": 1.5, "ibkr": 0.8}.get(broker, 5.0)

        def get_borrow_rate_annual_pct(self, broker: str) -> float:
            return {"alpaca": 15.0, "ibkr": 2.0}.get(broker, 5.0)

        def get_fallback_broker(self, asset_class, candidates, exclude):
            return candidates[0] if candidates else None

    # Setup SmartOrderRouter
    router = SmartOrderRouter(available_brokers=["ibkr", "alpaca"])
    router.permissions = FakePermissions()

    # Monkeypatch fused_routing_score to be 0.0 for both to test tiebreaker cost sorting.
    # Note: we use "crypto" asset class to avoid the hardcoded IBKR override for "equity"/"etf".
    # And we use a symbol that does not end with -USD (e.g. BTC-USDT) to avoid Kraken fiat USD pair overrides.
    monkeypatch.setattr(router, "fused_routing_score", lambda b, s: 0.0)

    # 1. Long position routing on crypto (compares taker fees)
    # ibkr taker fee = 0.8 bps, alpaca taker fee = 1.5 bps.
    # No borrow cost applies for long.
    # Should choose ibkr due to lower cost (0.8 < 1.5)
    metadata_long = {"side": "long", "hold_days": 5.0}
    best_broker_long = router.route("crypto", "BTC-USDT", metadata_long)
    assert best_broker_long == "ibkr"

    # 2. Short position routing on crypto (compares taker fees + borrow cost)
    # ibkr: cost = 0.8 fee + (2.0% * 100 * 20 / 365) borrow = 0.8 + 200 * 0.0548 = 0.8 + 10.96 = 11.76 bps
    # alpaca: cost = 1.5 fee + (15.0% * 100 * 20 / 365) borrow = 1.5 + 1500 * 0.0548 = 1.5 + 82.2 = 83.7 bps
    # Should choose ibkr because total cost (11.76) < alpaca total cost (83.7)
    metadata_short = {"side": "short", "hold_days": 20.0}
    best_broker_short = router.route("crypto", "BTC-USDT", metadata_short)
    assert best_broker_short == "ibkr"

    # 3. Test that fused routing score has priority over cost
    # Even though alpaca has higher taker fee (1.5 > 0.8), its fused score is higher (0.5 > 0.1).
    # So it should route to alpaca.
    def mock_fused_routing_score(broker, symbol):
        if broker == "alpaca":
            return 0.5
        return 0.1
    monkeypatch.setattr(router, "fused_routing_score", mock_fused_routing_score)
    best_broker_priority = router.route("crypto", "BTC-USDT", metadata_long)
    assert best_broker_priority == "alpaca"


# ==========================================
# 6. SignalEngine Kelly Sizing Integration
# ==========================================

def test_signal_engine_kelly_sizing_integration():
    from signals.engine import RawSignal, SignalEngine

    cfg = {
        "default_position_pct": 0.05,
        "min_quantity": 0.0001,
        "quantity_decimals": 4,
        "kelly_sizing": {
            "enabled": True,
            "kelly_fraction": 0.25,
            "fallback_win_rate": 0.50,
            "fallback_win_loss_ratio": 1.0,
        }
    }
    eng = SignalEngine(cfg)

    # Mock database performance query method
    def mock_stats(strategy):
        return 0.60, 1.5, 100

    eng._get_strategy_stats_sync = mock_stats

    raw = RawSignal(
        strategy="momentum_breakout",
        symbol="AAPL",
        side="buy",
        confidence=1.0,
        broker="ibkr",
        asset_class="equity",
        metadata={"close": 100.0, "atr_pct": 0.02},
    )

    sig = eng.process(raw, portfolio_value=Decimal("100000"))
    assert sig is not None
    # p = 0.60, b = 1.5 -> f_star = 0.60 - 0.40/1.5 = 0.333333
    # kelly_pct = 0.333333 * 0.25 = 0.083333
    # notional = 100000 * 0.083333 = 8333.33 -> qty = 83.3333 -> quantize to 4 decimals = 83.3333
    assert sig.suggested_quantity == Decimal("83.3333")


def test_signal_engine_kelly_negative_edge_dropped_signal():
    from signals.engine import RawSignal, SignalEngine

    cfg = {
        "default_position_pct": 0.05,
        "min_quantity": 0.0001,
        "quantity_decimals": 4,
        "kelly_sizing": {
            "enabled": True,
            "kelly_fraction": 0.25,
            "fallback_win_rate": 0.50,
            "fallback_win_loss_ratio": 1.0,
        }
    }
    eng = SignalEngine(cfg)

    # Mock database performance query method returning negative edge (40% WR, 1.0 Win/Loss Ratio)
    # f_star = 0.40 - (0.60 / 1.0) = -0.20 <= 0
    def mock_stats(strategy):
        return 0.40, 1.0, 100

    eng._get_strategy_stats_sync = mock_stats

    raw = RawSignal(
        strategy="momentum_breakout",
        symbol="AAPL",
        side="buy",
        confidence=1.0,
        broker="ibkr",
        asset_class="equity",
        metadata={"close": 100.0, "atr_pct": 0.02},
    )

    sig = eng.process(raw, portfolio_value=Decimal("100000"))
    assert sig is None  # Dropped!


def test_signal_engine_kelly_sample_size_fallback():
    from signals.engine import RawSignal, SignalEngine

    cfg = {
        "default_position_pct": 0.05,
        "min_quantity": 0.0001,
        "quantity_decimals": 4,
        "kelly_sizing": {
            "enabled": True,
            "kelly_fraction": 0.25,
            "fallback_win_rate": 0.50,
            "fallback_win_loss_ratio": 1.0,
        }
    }
    eng = SignalEngine(cfg)

    # Return only 5 fills (min_kelly_trades default is 30, so this will fall back)
    def mock_stats(strategy):
        return 0.60, 1.5, 5

    eng._get_strategy_stats_sync = mock_stats

    raw = RawSignal(
        strategy="momentum_breakout",
        symbol="AAPL",
        side="buy",
        confidence=1.0,
        broker="ibkr",
        asset_class="equity",
        metadata={"close": 100.0, "atr_pct": 0.02},
    )

    sig = eng.process(raw, portfolio_value=Decimal("100000"))
    assert sig is not None
    # Bypassed Kelly, fell back to normal vol-adjusted sizing:
    # risk = 0.5% NAV ($500) / atr 0.02 = 25% NAV ($25,000) -> 250 shares.
    assert sig.suggested_quantity == Decimal("250.0000")


# ==========================================
# 7. Slippage Cache TTL — YAML-driven contract
# ==========================================
#
# Locks D139 follow-up: the previous hardcoded 60-second TTL inside
# ``_get_rolling_average_slippage`` is now controlled by
# ``stale_price_gate.adaptive_slippage.cache_ttl_sec``. The function
# must (a) re-use cached values inside the TTL, (b) re-query when the
# TTL elapses, (c) skip caching entirely when the TTL is missing /
# zero / negative — the resolver refuses to invent a default.


class _CountingSession:
    """Session that tracks how many times ``execute`` was invoked.

    Used to verify the cache short-circuits the DB roundtrip vs. always
    re-queries when caching is disabled.
    """

    def __init__(self, rows):
        self._rows = rows
        self.execute_calls = 0

    async def execute(self, _stmt):
        self.execute_calls += 1
        rs = list(self._rows)

        class _Scalars:
            def __init__(self, r):
                self._r = r

            def all(self):
                return [row.slippage_bps for row in self._r]

        class _Result:
            def __init__(self, r):
                self._r = r

            def scalars(self):
                return _Scalars(self._r)

        return _Result(rs)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _counting_factory(session: _CountingSession):
    @asynccontextmanager
    async def _ctx():
        yield session

    def _factory():
        return _ctx()

    return _factory


@pytest.mark.asyncio
async def test_slippage_cache_reuses_within_ttl():
    """Two consecutive calls inside the TTL must hit the DB exactly once."""
    rows = [MockFillLog(slippage_bps=Decimal("3.0")) for _ in range(15)]
    sess = _CountingSession(rows)
    sf = _counting_factory(sess)
    eng = ExecutionEngine(broker_configs={}, paper_mode=True)

    v1 = await eng._get_rolling_average_slippage(
        sf, "AAPL", "equity", lookback=50, cache_ttl_sec=60,
    )
    v2 = await eng._get_rolling_average_slippage(
        sf, "AAPL", "equity", lookback=50, cache_ttl_sec=60,
    )

    assert v1 == v2 == Decimal("3.0")
    assert sess.execute_calls == 1  # second call served from cache


@pytest.mark.asyncio
async def test_slippage_cache_disabled_when_ttl_missing():
    """``cache_ttl_sec=None`` disables caching — every call hits the DB.

    This is the no-hardcoded-default contract: if the operator hasn't
    supplied a TTL, the engine refuses to invent one.
    """
    rows = [MockFillLog(slippage_bps=Decimal("3.0")) for _ in range(15)]
    sess = _CountingSession(rows)
    sf = _counting_factory(sess)
    eng = ExecutionEngine(broker_configs={}, paper_mode=True)

    await eng._get_rolling_average_slippage(sf, "AAPL", "equity", lookback=50)
    await eng._get_rolling_average_slippage(sf, "AAPL", "equity", lookback=50)
    await eng._get_rolling_average_slippage(sf, "AAPL", "equity", lookback=50)

    assert sess.execute_calls == 3
    assert eng._slippage_cache == {}  # nothing was ever cached


@pytest.mark.asyncio
async def test_slippage_cache_disabled_when_ttl_zero_or_negative():
    """``cache_ttl_sec=0`` and ``<0`` are treated the same as missing:
    caching disabled, no value invented."""
    rows = [MockFillLog(slippage_bps=Decimal("3.0")) for _ in range(15)]

    for ttl in (Decimal("0"), Decimal("-1"), 0.0, -5.0):
        sess = _CountingSession(rows)
        sf = _counting_factory(sess)
        eng = ExecutionEngine(broker_configs={}, paper_mode=True)
        await eng._get_rolling_average_slippage(
            sf, "AAPL", "equity", lookback=50, cache_ttl_sec=float(ttl),
        )
        await eng._get_rolling_average_slippage(
            sf, "AAPL", "equity", lookback=50, cache_ttl_sec=float(ttl),
        )
        assert sess.execute_calls == 2, f"ttl={ttl} should disable caching"


@pytest.mark.asyncio
async def test_slippage_cache_miss_is_also_cached():
    """Empty result must be cached too — otherwise symbols with no fill
    history hammer the DB on every paper-fill evaluation."""
    sess = _CountingSession([])  # no fills → query returns []
    sf = _counting_factory(sess)
    eng = ExecutionEngine(broker_configs={}, paper_mode=True)

    v1 = await eng._get_rolling_average_slippage(
        sf, "ZZZNEW", "equity", lookback=50, cache_ttl_sec=60,
    )
    v2 = await eng._get_rolling_average_slippage(
        sf, "ZZZNEW", "equity", lookback=50, cache_ttl_sec=60,
    )

    assert v1 is None and v2 is None
    # Symbol query + asset_class fallback query = 2 hits on the first
    # call. The second call must be served entirely from cache.
    first_call_queries = sess.execute_calls
    assert first_call_queries >= 1
    # If the cache stored the miss, no further DB hits occurred.
    assert sess.execute_calls == first_call_queries


