"""Smoke: D015 engine modules import and stub pipeline runs end-to-end."""

from datetime import datetime, timezone
from decimal import Decimal

from config.loaders import load_allocation, load_profile_modes
from core.models_runtime import PortfolioState, SignalCandidate
from execution.planner import build_execution_plan
from portfolio.allocation_engine import build_allocation_decision
from risk.regime_state import compute_regime_state
from signals.opportunity_engine import build_opportunities


def test_d015_stub_pipeline() -> None:
    alloc = load_allocation()
    profile = load_profile_modes()
    now = datetime.now(timezone.utc)

    portfolio = PortfolioState(
        timestamp=now,
        mode="trader",
        nav=Decimal("100000"),
        cash=Decimal("40000"),
        available_buying_power=Decimal("60000"),
        gross_exposure=Decimal("60000"),
        net_exposure=Decimal("60000"),
        leverage_ratio=Decimal("1"),
    )

    regime = compute_regime_state(portfolio_state=portfolio, allocation_cfg=alloc, now=now)
    assert regime.regime_label == "insufficient_data"

    candidates = [
        SignalCandidate(
            symbol="BTC-USD",
            asset_class="crypto",
            side="buy",
            timestamp=now,
            raw_signal_strength=Decimal("0.7"),
            adjusted_signal_strength=Decimal("0.65"),
            confidence=Decimal("0.72"),
            strategy_name="momentum",
        )
    ]
    opps = build_opportunities(
        signals=candidates,
        regime_state=regime,
        allocation_cfg=alloc,
        profile_cfg=profile,
        active_profile_mode="trader",
        feature_json_by_symbol={
            "BTC-USD": {"vol_ratio": 2.4, "vpin_proxy_50": 0.35, "mom_10": 1.0},
        },
        now=now,
    )
    assert len(opps) == 1
    assert opps[0].symbol == "BTC-USD"
    assert opps[0].volume_flow is not None
    assert opps[0].components.volume_anomaly >= Decimal("0")
    assert opps[0].metadata.get("volume_refresh_context") is not None

    decision = build_allocation_decision(
        opportunities=opps,
        portfolio_state=portfolio,
        regime_state=regime,
        allocation_cfg=alloc,
        profile_cfg=profile,
        now=now,
    )
    assert decision.metadata.get("d015") is True

    plan = build_execution_plan(decision=decision, portfolio_state=portfolio, now=now)
    assert plan.metadata.get("d015") is True
    assert isinstance(plan.instructions, list)
