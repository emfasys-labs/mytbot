from datetime import datetime, timezone
from decimal import Decimal

from config.loaders import load_allocation, load_profile_modes
from core.models_runtime import Opportunity, OpportunityComponents, PortfolioState, RegimeState
from portfolio.allocation_engine import build_allocation_decision
from risk.regime_state import compute_regime_state_from_inputs


def _opp(sym: str, score: str) -> Opportunity:
    return Opportunity(
        symbol=sym,
        asset_class="crypto",
        side="long",
        timestamp=datetime.now(timezone.utc),
        opportunity_score=Decimal(score),
        urgency_score=Decimal("0.5"),
        confidence=Decimal("0.7"),
        components=OpportunityComponents(momentum=Decimal(score)),
    )


def test_allocation_produces_targets_and_bounded_ge() -> None:
    alloc = load_allocation()
    profile = load_profile_modes()
    regime = compute_regime_state_from_inputs(
        portfolio_state=PortfolioState(
            timestamp=datetime.now(timezone.utc),
            mode="trader",
            nav=Decimal("100000"),
            cash=Decimal("20000"),
            available_buying_power=Decimal("80000"),
            gross_exposure=Decimal("80000"),
            net_exposure=Decimal("80000"),
            leverage_ratio=Decimal("1"),
            metadata={"capital_pct": 0.8},
        ),
        allocation_cfg=alloc,
        feature_rows=[
            {"symbol": "A", "features": {"mom_10": 1.0, "volume_z": 0.5, "relative_dollar_volume": 1.1}},
            {"symbol": "B", "features": {"mom_10": 1.0, "volume_z": 0.5, "relative_dollar_volume": 1.1}},
            {"symbol": "C", "features": {"mom_10": 1.0, "volume_z": 0.5, "relative_dollar_volume": 1.1}},
        ],
        news_dispersion=None,
    )
    ps = PortfolioState(
        timestamp=datetime.now(timezone.utc),
        mode="trader",
        nav=Decimal("100000"),
        cash=Decimal("20000"),
        available_buying_power=Decimal("80000"),
        gross_exposure=Decimal("80000"),
        net_exposure=Decimal("80000"),
        leverage_ratio=Decimal("1"),
        metadata={"capital_pct": 0.8},
    )
    opps = [_opp("BTC-USD", "0.8"), _opp("ETH-USD", "0.55")]
    dec = build_allocation_decision(
        opportunities=opps,
        portfolio_state=ps,
        regime_state=regime,
        allocation_cfg=alloc,
        profile_cfg=profile,
    )
    assert dec.gross_exposure_target > Decimal("0")
    assert dec.gross_exposure_target <= Decimal("3")
    assert len(dec.allocation_targets) == 2
