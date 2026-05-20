from datetime import datetime, timezone
from decimal import Decimal

from config.loaders import load_allocation, load_profile_modes
from core.models_runtime import Opportunity, OpportunityComponents, PortfolioState, RegimeState
from portfolio.allocation_engine import build_allocation_decision


def _opp(sym: str, score: str) -> Opportunity:
    return Opportunity(
        symbol=sym,
        asset_class="equity",
        side="long",
        timestamp=datetime.now(timezone.utc),
        opportunity_score=Decimal(score),
        urgency_score=Decimal("0.5"),
        confidence=Decimal("1.0"),
        price=Decimal("100.0"),
        components=OpportunityComponents(
            momentum=Decimal(score),
            liquidity_quality=Decimal("1.0"),
        ),
        metadata={"atr_14": 1.0, "close": 100.0},
    )


def test_allocation_unleash_factors() -> None:
    alloc = load_allocation()
    profile = load_profile_modes()

    # Setup a dampening regime (e.g. crash/volatile or with execution/drawdown throttles < 1.0)
    regime = RegimeState(
        timestamp=datetime.now(timezone.utc),
        regime_label="volatile",
        market_state_score=Decimal("-0.5"),
        drawdown_throttle=Decimal("0.4"),
        execution_quality=Decimal("0.5"),
        breadth_score=Decimal("-0.3"),
    )

    opps = [_opp("AAPL", "0.2"), _opp("MSFT", "0.1")]

    # 1. Slider = 0.9 (Should NOT unleash, u = 0.0)
    ps_90 = PortfolioState(
        timestamp=datetime.now(timezone.utc),
        mode="hunter",
        nav=Decimal("100000"),
        cash=Decimal("100000"),
        available_buying_power=Decimal("200000"),
        gross_exposure=Decimal("0"),
        net_exposure=Decimal("0"),
        leverage_ratio=Decimal("1"),
        metadata={"capital_pct": 0.9},
    )
    dec_90 = build_allocation_decision(
        opportunities=opps,
        portfolio_state=ps_90,
        regime_state=regime,
        allocation_cfg=alloc,
        profile_cfg=profile,
    )
    assert dec_90.metadata.get("unleash_u") == 0.0
    # Gross exposure target should be significantly throttled down due to u=0
    assert dec_90.gross_exposure_target < Decimal("0.3")

    # 2. Slider = 1.0 (Should fully unleash, u = 1.0)
    ps_100 = PortfolioState(
        timestamp=datetime.now(timezone.utc),
        mode="hunter",
        nav=Decimal("100000"),
        cash=Decimal("100000"),
        available_buying_power=Decimal("200000"),
        gross_exposure=Decimal("0"),
        net_exposure=Decimal("0"),
        leverage_ratio=Decimal("1"),
        metadata={"capital_pct": 1.0},
    )
    dec_100 = build_allocation_decision(
        opportunities=opps,
        portfolio_state=ps_100,
        regime_state=regime,
        allocation_cfg=alloc,
        profile_cfg=profile,
    )
    assert dec_100.metadata.get("unleash_u") == 1.0
    # All dampening multipliers (agg, ge_shape, eq, dt, vol_overlay) should become exactly 1.0
    # Therefore, ge = cap_slider * 1 * 1 * 1 * 1 * 1 = 1.0
    assert dec_100.gross_exposure_target == Decimal("1.0")

    # 3. Slider = 0.95 (Should partially unleash, u = 0.5)
    ps_95 = PortfolioState(
        timestamp=datetime.now(timezone.utc),
        mode="hunter",
        nav=Decimal("100000"),
        cash=Decimal("100000"),
        available_buying_power=Decimal("200000"),
        gross_exposure=Decimal("0"),
        net_exposure=Decimal("0"),
        leverage_ratio=Decimal("1"),
        metadata={"capital_pct": 0.95},
    )
    dec_95 = build_allocation_decision(
        opportunities=opps,
        portfolio_state=ps_95,
        regime_state=regime,
        allocation_cfg=alloc,
        profile_cfg=profile,
    )
    assert dec_95.metadata.get("unleash_u") == 0.5
    # The target should be between the 90% and 100% outputs, scaled by the interpolated factors.
    assert dec_95.gross_exposure_target > dec_90.gross_exposure_target
    assert dec_95.gross_exposure_target < dec_100.gross_exposure_target
