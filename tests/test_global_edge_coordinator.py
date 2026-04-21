from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from portfolio.global_edge_coordinator import (
    CoordinatorAction,
    GlobalEdgeCoordinator,
    HeldPositionEdge,
    signal_candidate_to_strategy_opportunity,
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


class _FakeSignalCandidate:
    """Minimal SignalCandidate-shaped object for the round-trip test."""

    def __init__(self, symbol: str, asset_class: str, metadata: dict | None = None) -> None:
        self.symbol = symbol
        self.asset_class = asset_class
        self.side = "long"
        self.strategy_name = "mean_reversion"
        self.adjusted_signal_strength = Decimal("0.2")
        self.confidence = Decimal("0.65")
        self.timestamp = datetime.now(timezone.utc)
        self.metadata = metadata or {}


def test_signal_candidate_preserves_asset_class_through_strategy_opportunity() -> None:
    """Regression: SignalCandidate.asset_class must survive into StrategyOpportunity.metadata.

    Without this, the D015 coordinator path strips the true asset class and
    ``coordinator_action_to_raw_signal`` defaults to "equity", causing crypto
    / forex / futures signals to be routed to the wrong broker and persisted
    with the wrong label.
    """
    for ac in ("crypto", "forex", "future", "equity"):
        cand = _FakeSignalCandidate("SOL-USD", asset_class=ac)
        opp = signal_candidate_to_strategy_opportunity(
            cand,
            nav=Decimal("100000"),
            position_pct=Decimal("0.05"),
            price=Decimal("150"),
        )
        assert opp is not None, f"opp should be built for {ac}"
        assert opp.metadata.get("asset_class") == ac


def test_signal_candidate_metadata_asset_class_is_not_overwritten() -> None:
    """If the candidate already carries an explicit asset_class in metadata, respect it."""
    cand = _FakeSignalCandidate(
        "SOL-USD",
        asset_class="equity",
        metadata={"asset_class": "crypto"},
    )
    opp = signal_candidate_to_strategy_opportunity(
        cand,
        nav=Decimal("100000"),
        position_pct=Decimal("0.05"),
        price=Decimal("150"),
    )
    assert opp is not None
    assert opp.metadata.get("asset_class") == "crypto"


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
