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
    # Negative edge, f_star <= 0. Should fallback to min notional (0.5% or 0.005 NAV)
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
    assert decision.path == "kelly_zero_edge_fallback"
    assert decision.notional == Decimal("100000") * Decimal("0.005")  # Min notional pct = 0.5%


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

    # 1. Long position routing on equity (compares taker fees)
    # ibkr taker fee = 0.8 bps, alpaca taker fee = 1.5 bps.
    # No borrow cost applies for long.
    # Should choose ibkr due to lower cost (0.8 < 1.5)
    metadata_long = {"side": "long", "hold_days": 5.0}
    best_broker_long = router.route("equity", "AAPL", metadata_long)
    assert best_broker_long == "ibkr"

    # 2. Short position routing on equity (compares taker fees + borrow cost)
    # ibkr: cost = 0.8 fee + (2.0% * 100 * 20 / 365) borrow = 0.8 + 200 * 0.0548 = 0.8 + 10.96 = 11.76 bps
    # alpaca: cost = 1.5 fee + (15.0% * 100 * 20 / 365) borrow = 1.5 + 1500 * 0.0548 = 1.5 + 82.2 = 83.7 bps
    # Should choose ibkr because total cost (11.76) < alpaca total cost (83.7)
    metadata_short = {"side": "short", "hold_days": 20.0}
    best_broker_short = router.route("equity", "AAPL", metadata_short)
    assert best_broker_short == "ibkr"


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
        return 0.60, 1.5

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

