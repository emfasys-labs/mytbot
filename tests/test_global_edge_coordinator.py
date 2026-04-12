from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from portfolio.global_edge_coordinator import (
    CoordinatorAction,
    GlobalEdgeCoordinator,
    HeldPositionEdge,
)
from portfolio.strategy_opportunity import StrategyOpportunity, compute_priority_score


def _opp(
    symbol: str,
    edge: str,
    cap: str = "10000",
) -> StrategyOpportunity:
    e = Decimal(edge)
    conf = Decimal("0.9")
    reg = Decimal("0.85")
    exe = Decimal("0.8")
    risk = Decimal("0.05")
    ps = compute_priority_score(e, conf, reg, exe, risk)
    return StrategyOpportunity(
        strategy_name="momentum_breakout",
        symbol=symbol,
        side="long",
        created_at=datetime.now(timezone.utc),
        expected_edge=e,
        confidence=conf,
        capital_required=Decimal(cap),
        expected_holding_hours=24,
        liquidity_score=Decimal("0.8"),
        execution_score=exe,
        regime_fit_score=reg,
        risk_cost_score=risk,
        priority_score=ps,
        metadata={},
    )


def test_propose_skips_when_edge_below_weakest_plus_threshold() -> None:
    cfg = {
        "edge_advantage": {"trader": "0.05"},
        "max_actions_per_tick": 3,
        "max_notional_fraction_per_action": "1.0",
    }
    coord = GlobalEdgeCoordinator(cfg)
    held = [HeldPositionEdge(symbol="AAA", notional=Decimal("1000"), expected_remaining_edge=Decimal("0.20"))]
    new_opps = [_opp("BBB", "0.22")]  # 0.22 <= 0.20 + 0.05
    actions = coord.propose_actions(held, new_opps, active_mode="trader")
    assert actions == []


def test_propose_emits_open_only_incremental() -> None:
    cfg = {
        "edge_advantage": {"trader": "0.05"},
        "max_actions_per_tick": 3,
        "max_notional_fraction_per_action": "0.5",
    }
    coord = GlobalEdgeCoordinator(cfg)
    held = [HeldPositionEdge(symbol="AAA", notional=Decimal("1000"), expected_remaining_edge=Decimal("0.10"))]
    new_opps = [_opp("BBB", "0.30")]
    actions = coord.propose_actions(held, new_opps, active_mode="trader")
    assert len(actions) == 1
    a = actions[0]
    assert isinstance(a, CoordinatorAction)
    assert a.kind == "open_strategy"
    assert a.symbol == "BBB"
    # Incremental: cap = 10000 * 0.5 = 5000
    assert a.capital == Decimal("5000")


def test_no_close_all_action_kind() -> None:
    cfg = {
        "edge_advantage": {"trader": "0.01"},
        "max_actions_per_tick": 5,
        "max_notional_fraction_per_action": "1.0",
    }
    coord = GlobalEdgeCoordinator(cfg)
    held = [
        HeldPositionEdge(symbol="A", notional=Decimal("1"), expected_remaining_edge=Decimal("0.01")),
        HeldPositionEdge(symbol="B", notional=Decimal("1"), expected_remaining_edge=Decimal("0.01")),
    ]
    new_opps = [_opp("C", "0.5"), _opp("D", "0.4")]
    actions = coord.propose_actions(held, new_opps, active_mode="trader")
    assert all(x.kind == "open_strategy" for x in actions)
