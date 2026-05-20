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
    max_w = compute_adaptive_max_weight(opp, regime, "hunter", cfg, target_risk_budget=0.015)
    
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
    max_w = compute_adaptive_max_weight(opp, regime, "hunter", cfg, target_risk_budget=0.015)
    
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
    max_w = compute_adaptive_max_weight(opp, regime, "hunter", cfg, target_risk_budget=0.015)
    
    # Vol cap = 1.0, liq = 1.0, crash = 1.0 * 0.25 = 0.25
    assert max_w == Decimal("0.25")
