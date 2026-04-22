from datetime import datetime, timezone
from decimal import Decimal

from core.models_runtime import AllocationDecision, AllocationTarget, PortfolioState
from execution.planner import build_execution_plan


def _portfolio() -> PortfolioState:
    return PortfolioState(
        timestamp=datetime.now(timezone.utc),
        mode="trader",
        nav=Decimal("100000"),
        cash=Decimal("30000"),
        available_buying_power=Decimal("70000"),
        gross_exposure=Decimal("70000"),
        net_exposure=Decimal("70000"),
        leverage_ratio=Decimal("1"),
    )


def _decision(demand_score: float) -> AllocationDecision:
    now = datetime.now(timezone.utc)
    return AllocationDecision(
        timestamp=now,
        mode="trader",
        gross_exposure_target=Decimal("1.0"),
        net_exposure_target=Decimal("1.0"),
        capital_deployment_target=Decimal("60000"),
        allocation_targets=[
            AllocationTarget(
                symbol="SPY",
                target_weight=Decimal("0.5"),
                target_notional=Decimal("50000"),
                target_leverage=Decimal("1"),
                side="long",
                source_opportunity_score=Decimal("0.8"),
                priority_rank=1,
            )
        ],
        open_symbols=["SPY"],
        close_symbols=[],
        hold_symbols=[],
        replacement_candidates=[],
        rationale="test",
        metadata={"d015": True, "demand_score": demand_score},
    )


def test_planner_urgency_increases_for_aligned_demand_open() -> None:
    plan_pos = build_execution_plan(decision=_decision(0.8), portfolio_state=_portfolio())
    plan_neg = build_execution_plan(decision=_decision(-0.8), portfolio_state=_portfolio())
    up = next(i for i in plan_pos.instructions if i.symbol == "SPY")
    dn = next(i for i in plan_neg.instructions if i.symbol == "SPY")
    assert up.urgency_score > dn.urgency_score
