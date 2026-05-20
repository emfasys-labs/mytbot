import pytest
from decimal import Decimal
from datetime import datetime, timezone

from core.models_runtime import Opportunity, RegimeState, OpportunityComponents
from portfolio.adaptive_sizing import compute_adaptive_max_weight

# Mock config
class MockSafetyBounds:
    def __init__(self):
        self.absolute_max_single_position_weight = {"hunter": 1.0, "trader": 0.5, "defender": 0.2}

class MockProfileConfig:
    def __init__(self):
        self.safety_bounds = MockSafetyBounds()

def test_adaptive_sizing_high_volatility():
    # If volatility is very high (atr=10, price=100 -> 10%)
    # Target budget is 1.5%, so max weight should be 0.015 / 0.10 = 0.15
    opp = Opportunity(
        symbol="VOL",
        asset_class="equity",
        side="long",
        timestamp=datetime.now(timezone.utc),
        opportunity_score=Decimal("0.9"),
        urgency_score=Decimal("0.5"),
        confidence=Decimal("1.0"),
        price=Decimal("100.0"),
        components=OpportunityComponents(liquidity_quality=Decimal("1.0")),
        metadata={"atr_14": 10.0, "close": 100.0}
    )
    regime = RegimeState(
        timestamp=datetime.now(timezone.utc),
        regime_label="trend_up",
        market_state_score=Decimal("0.8"),
        drawdown_throttle=Decimal("1.0"),
        execution_quality=Decimal("1.0"),
        breadth_score=Decimal("0.5")
    )
    
    cfg = MockProfileConfig()
    max_w = compute_adaptive_max_weight(opp, regime, None, "hunter", cfg, target_risk_budget=0.015)
    
    assert max_w == Decimal("0.15")

def test_adaptive_sizing_low_liquidity():
    # Low liquidity quality (0.2)
    # Vol is low (atr=1, price=100 -> 1%) -> Vol cap = 1.0 (since 0.015/0.01 = 1.5, min(1.0, 1.5) = 1.0)
    # Liquidity cap = 1.0 * (0.2 * 1.5) = 0.3
    opp = Opportunity(
        symbol="ILLIQ",
        asset_class="equity",
        side="long",
        timestamp=datetime.now(timezone.utc),
        opportunity_score=Decimal("0.9"),
        urgency_score=Decimal("0.5"),
        confidence=Decimal("1.0"),
        price=Decimal("100.0"),
        components=OpportunityComponents(liquidity_quality=Decimal("0.2")),
        metadata={"atr_14": 1.0, "close": 100.0}
    )
    regime = RegimeState(
        timestamp=datetime.now(timezone.utc),
        regime_label="trend_up",
        market_state_score=Decimal("0.8"),
        drawdown_throttle=Decimal("1.0"),
        execution_quality=Decimal("1.0"),
        breadth_score=Decimal("0.5")
    )
    
    cfg = MockProfileConfig()
    max_w = compute_adaptive_max_weight(opp, regime, None, "hunter", cfg, target_risk_budget=0.015)
    
    # 0.2 * 1.5 = 0.3
    assert max_w == Decimal("0.3")

def test_adaptive_sizing_crash_regime():
    opp = Opportunity(
        symbol="SPY",
        asset_class="equity",
        side="long",
        timestamp=datetime.now(timezone.utc),
        opportunity_score=Decimal("0.9"),
        urgency_score=Decimal("0.5"),
        confidence=Decimal("1.0"),
        price=Decimal("100.0"),
        components=OpportunityComponents(liquidity_quality=Decimal("1.0")),
        metadata={"atr_14": 1.0, "close": 100.0}
    )
    regime = RegimeState(
        timestamp=datetime.now(timezone.utc),
        regime_label="crash",
        market_state_score=Decimal("0.8"),
        drawdown_throttle=Decimal("1.0"),
        execution_quality=Decimal("1.0"),
        breadth_score=Decimal("0.5")
    )
    
    cfg = MockProfileConfig()
    max_w = compute_adaptive_max_weight(opp, regime, None, "hunter", cfg, target_risk_budget=0.015)
    
    # Vol cap = 1.0, liq = 1.0, crash = 1.0 * 0.25 = 0.25
    assert max_w == Decimal("0.25")

def test_adaptive_sizing_unleash():
    from core.models_runtime import PortfolioState
    opp = Opportunity(
        symbol="SPY",
        asset_class="equity",
        side="long",
        timestamp=datetime.now(timezone.utc),
        opportunity_score=Decimal("0.9"),
        urgency_score=Decimal("0.5"),
        confidence=Decimal("1.0"),
        price=Decimal("100.0"),
        components=OpportunityComponents(liquidity_quality=Decimal("1.0")),
        metadata={"atr_14": 1.0, "close": 100.0}
    )
    regime = RegimeState(
        timestamp=datetime.now(timezone.utc),
        regime_label="crash",
        market_state_score=Decimal("0.8"),
        drawdown_throttle=Decimal("1.0"),
        execution_quality=Decimal("1.0"),
        breadth_score=Decimal("0.5")
    )
    cfg = MockProfileConfig()

    # 1. Conservative baseline: capital_pct = 0.9 (Should not unleash, u = 0)
    pstate_90 = PortfolioState(
        timestamp=datetime.now(timezone.utc), mode="hunter",
        nav=Decimal("100000"), cash=Decimal("100000"),
        available_buying_power=Decimal("200000"), gross_exposure=Decimal("0"),
        net_exposure=Decimal("0"), leverage_ratio=Decimal("1"),
        metadata={"capital_pct": 0.9}
    )
    max_w_90 = compute_adaptive_max_weight(opp, regime, pstate_90, "hunter", cfg, target_risk_budget=0.015)
    # With u = 0, should be same as baseline: 0.25
    assert max_w_90 == Decimal("0.25")

    # 2. Fully unleashed: capital_pct = 1.0
    pstate_100 = PortfolioState(
        timestamp=datetime.now(timezone.utc), mode="hunter",
        nav=Decimal("100000"), cash=Decimal("100000"),
        available_buying_power=Decimal("200000"), gross_exposure=Decimal("0"),
        net_exposure=Decimal("0"), leverage_ratio=Decimal("1"),
        metadata={"capital_pct": 1.0}
    )
    max_w_100 = compute_adaptive_max_weight(opp, regime, pstate_100, "hunter", cfg, target_risk_budget=0.015)
    # u = 1.0, should interpolate to nuclear_max (1.0)
    assert max_w_100 == Decimal("1.0000")

    # 3. Partially unleashed: capital_pct = 0.95
    pstate_95 = PortfolioState(
        timestamp=datetime.now(timezone.utc), mode="hunter",
        nav=Decimal("100000"), cash=Decimal("100000"),
        available_buying_power=Decimal("200000"), gross_exposure=Decimal("0"),
        net_exposure=Decimal("0"), leverage_ratio=Decimal("1"),
        metadata={"capital_pct": 0.95}
    )
    max_w_95 = compute_adaptive_max_weight(opp, regime, pstate_95, "hunter", cfg, target_risk_budget=0.015)
    # u = (0.95 - 0.9) / 0.1 = 0.5
    # final_cap = 0.25 * (1 - 0.5) + 1.0 * 0.5 = 0.125 + 0.50 = 0.625
    assert max_w_95 == Decimal("0.6250")

