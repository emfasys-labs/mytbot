from datetime import datetime, timezone
from decimal import Decimal

from config.loaders import load_allocation, load_profile_modes
from core.models_runtime import Opportunity, OpportunityComponents, PortfolioState
from portfolio.allocation_engine import build_allocation_decision
from risk.regime_state import compute_regime_state_from_inputs


def _opp(score: str) -> Opportunity:
    return Opportunity(
        symbol="SPY",
        asset_class="equity",
        side="long",
        timestamp=datetime.now(timezone.utc),
        opportunity_score=Decimal(score),
        urgency_score=Decimal("0.5"),
        confidence=Decimal("0.7"),
        components=OpportunityComponents(momentum=Decimal(score)),
    )


def _ps() -> PortfolioState:
    return PortfolioState(
        timestamp=datetime.now(timezone.utc),
        mode="trader",
        nav=Decimal("100000"),
        cash=Decimal("20000"),
        available_buying_power=Decimal("80000"),
        gross_exposure=Decimal("80000"),
        net_exposure=Decimal("80000"),
        leverage_ratio=Decimal("1"),
        metadata={"capital_pct": 1.0},
    )


def test_high_market_volatility_reduces_gross_exposure_target() -> None:
    alloc = load_allocation()
    profile = load_profile_modes()
    ps = _ps()
    base_regime = compute_regime_state_from_inputs(
        portfolio_state=ps,
        allocation_cfg=alloc,
        feature_rows=[
            {"symbol": "A", "features": {"mom_10": 1.0, "volume_z": 0.4, "relative_dollar_volume": 1.0}},
            {"symbol": "B", "features": {"mom_10": 0.9, "volume_z": 0.3, "relative_dollar_volume": 1.1}},
            {"symbol": "C", "features": {"mom_10": 0.7, "volume_z": 0.2, "relative_dollar_volume": 1.2}},
        ],
        news_dispersion=None,
    )
    low_vol = base_regime
    low_vol.metadata = dict(low_vol.metadata or {})
    low_vol.metadata["market_volatility"] = 0.008
    low_vol.metadata["cross_asset_coverage"] = 1.0

    high_vol = compute_regime_state_from_inputs(
        portfolio_state=ps,
        allocation_cfg=alloc,
        feature_rows=[
            {"symbol": "A", "features": {"mom_10": 1.0, "volume_z": 0.4, "relative_dollar_volume": 1.0}},
            {"symbol": "B", "features": {"mom_10": 0.9, "volume_z": 0.3, "relative_dollar_volume": 1.1}},
            {"symbol": "C", "features": {"mom_10": 0.7, "volume_z": 0.2, "relative_dollar_volume": 1.2}},
        ],
        news_dispersion=None,
    )
    high_vol.metadata = dict(high_vol.metadata or {})
    high_vol.metadata["market_volatility"] = 0.035
    high_vol.metadata["cross_asset_coverage"] = 1.0

    d_low = build_allocation_decision(
        opportunities=[_opp("0.8")],
        portfolio_state=ps,
        regime_state=low_vol,
        allocation_cfg=alloc,
        profile_cfg=profile,
    )
    d_high = build_allocation_decision(
        opportunities=[_opp("0.8")],
        portfolio_state=ps,
        regime_state=high_vol,
        allocation_cfg=alloc,
        profile_cfg=profile,
    )
    assert d_high.gross_exposure_target <= d_low.gross_exposure_target
    assert "vol_overlay_applied" in d_high.metadata
