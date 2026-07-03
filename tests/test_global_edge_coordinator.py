from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from portfolio.global_edge_coordinator import (
    CoordinatorAction,
    GlobalEdgeCoordinator,
    HeldPositionEdge,
    held_positions_from_portfolio,
    signal_candidate_to_strategy_opportunity,
)
from portfolio.d015_replacement_context import ReplacementContext
from portfolio.strategy_opportunity import StrategyOpportunity, compute_priority_score


def _opp(
    symbol: str,
    edge: str,
    cap: str = "10000",
    *,
    side: str = "long",
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
        side=side,
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


def test_propose_emits_trim_then_open_incremental() -> None:
    cfg = {
        "edge_advantage": {"trader": "0.05"},
        "max_actions_per_tick": 3,
        "max_notional_fraction_per_action": "0.5",
    }
    coord = GlobalEdgeCoordinator(cfg)
    held = [HeldPositionEdge(symbol="AAA", notional=Decimal("1000"), expected_remaining_edge=Decimal("0.10"))]
    new_opps = [_opp("BBB", "0.30")]
    actions = coord.propose_actions(held, new_opps, active_mode="trader")
    assert len(actions) == 2
    trim, a = actions
    assert isinstance(trim, CoordinatorAction)
    assert trim.kind == "trim_symbol"
    assert trim.symbol == "AAA"
    assert trim.metadata["reduce_only"] is True
    assert isinstance(a, CoordinatorAction)
    assert a.kind == "open_strategy"
    assert a.symbol == "BBB"
    # Incremental: cap = 10000 * 0.5 = 5000
    assert a.capital == Decimal("5000")


def test_zero_allocation_flatten_emits_reduce_only_closes_largest_first() -> None:
    cfg = {
        "max_actions_per_tick": {"hunter": 2},
    }
    coord = GlobalEdgeCoordinator(cfg)
    held = [
        HeldPositionEdge(
            symbol="SMALL",
            notional=Decimal("1000"),
            expected_remaining_edge=Decimal("0.01"),
            broker="alpaca",
            metadata={"quantity": "10", "side": "long", "asset_class": "equity"},
        ),
        HeldPositionEdge(
            symbol="LARGE",
            notional=Decimal("250000"),
            expected_remaining_edge=Decimal("0.20"),
            broker="ibkr",
            metadata={"quantity": "-500", "side": "short", "asset_class": "equity"},
        ),
        HeldPositionEdge(
            symbol="MID",
            notional=Decimal("50000"),
            expected_remaining_edge=Decimal("0.02"),
            broker="kraken",
            metadata={"quantity": "2", "side": "long", "asset_class": "crypto"},
        ),
    ]

    actions = coord.propose_flatten_actions(held, active_mode="hunter", max_actions=2)

    assert [a.symbol for a in actions] == ["LARGE", "MID"]
    assert all(a.kind == "trim_symbol" for a in actions)
    assert all(a.strategy_name == "global_edge_flatten" for a in actions)
    assert all(a.metadata["reduce_only"] is True for a in actions)
    assert all(a.metadata["close_only"] is True for a in actions)
    assert all(a.metadata["flatten_all"] is True for a in actions)
    assert all(a.metadata["force_market_order"] is True for a in actions)
    assert actions[0].metadata["broker"] == "ibkr"
    assert actions[1].metadata["asset_class"] == "crypto"


def test_propose_skips_open_when_same_symbol_and_side_already_held() -> None:
    cfg = {
        "edge_advantage": {"trader": "0.05"},
        "max_actions_per_tick": 3,
        "max_notional_fraction_per_action": "1.0",
    }
    coord = GlobalEdgeCoordinator(cfg)
    held = [
        HeldPositionEdge(
            symbol="ETH-USD",
            notional=Decimal("5000"),
            expected_remaining_edge=Decimal("0.10"),
            metadata={"side": "long"},
        )
    ]
    new_opps = [_opp("ETH-USD", "0.50")]
    actions = coord.propose_actions(held, new_opps, active_mode="trader")
    assert actions == []


def test_propose_allows_open_when_same_symbol_opposite_side_held() -> None:
    cfg = {
        "edge_advantage": {"trader": "0.05"},
        "max_actions_per_tick": 3,
        "max_notional_fraction_per_action": "1.0",
    }
    coord = GlobalEdgeCoordinator(cfg)
    held = [
        HeldPositionEdge(
            symbol="ETH-USD",
            notional=Decimal("5000"),
            expected_remaining_edge=Decimal("0.10"),
            metadata={"side": "long"},
        )
    ]
    new_opps = [_opp("ETH-USD", "0.50", side="short")]
    actions = coord.propose_actions(held, new_opps, active_mode="trader")
    assert any(a.kind == "open_strategy" and a.symbol == "ETH-USD" for a in actions)


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


def test_signal_candidate_preserves_price_for_signal_engine_sizing() -> None:
    cand = _FakeSignalCandidate(
        "BTC-USD",
        asset_class="crypto",
        metadata={"target_notional": "5000"},
    )
    opp = signal_candidate_to_strategy_opportunity(
        cand,
        nav=Decimal("100000"),
        position_pct=Decimal("0.05"),
        price=Decimal("100000"),
    )

    assert opp is not None
    assert opp.metadata["close"] == "100000"
    assert opp.metadata["price"] == "100000"
    assert opp.metadata["side"] == "long"


def test_replacement_emits_trim_symbol_not_close_all() -> None:
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
    assert any(x.kind == "trim_symbol" for x in actions)
    assert all(x.kind in {"trim_symbol", "open_strategy"} for x in actions)
    assert all(x.kind != "close_all" for x in actions)


def test_hunter_rotation_replaces_weak_hold_when_edge_covers_fees() -> None:
    coord = GlobalEdgeCoordinator(
        {
            "rotation": {
                "enabled": True,
                "max_replacements_per_tick": {"hunter": 1},
                "min_edge_advantage": {"hunter": "0.01"},
                "estimated_round_trip_fee_bps": "20",
            },
            "cash_factors": {"equity": "1.0", "forex": "0.20"},
        }
    )
    held = [
        HeldPositionEdge(
            symbol="OLD",
            notional=Decimal("100000"),
            expected_remaining_edge=Decimal("0.05"),
            broker="ibkr",
            metadata={"side": "long", "asset_class": "equity"},
        )
    ]
    opp = StrategyOpportunity(
        strategy_name="volume_flow",
        symbol="NEW",
        side="long",
        created_at=datetime.now(timezone.utc),
        expected_edge=Decimal("0.80"),
        confidence=Decimal("0.80"),
        capital_required=Decimal("25000"),
        expected_holding_hours=6,
        liquidity_score=Decimal("0.8"),
        execution_score=Decimal("0.8"),
        regime_fit_score=Decimal("0.8"),
        risk_cost_score=Decimal("0.05"),
        priority_score=Decimal("0.20"),
        metadata={"asset_class": "equity"},
    )

    actions = coord.propose_rotation_actions(held, [opp], active_mode="hunter")

    assert [a.kind for a in actions] == ["trim_symbol", "open_strategy"]
    assert actions[0].symbol == "OLD"
    assert actions[0].metadata["rotation_replacement_symbol"] == "NEW"
    assert actions[1].symbol == "NEW"
    assert actions[1].capital == Decimal("100000.00")
    assert actions[1].metadata["sizing_path"] == "fee_aware_rotation"


def test_hunter_rotation_does_not_replace_when_fee_adjusted_edge_is_too_small() -> None:
    coord = GlobalEdgeCoordinator(
        {
            "rotation": {
                "enabled": True,
                "max_replacements_per_tick": {"hunter": 1},
                "min_edge_advantage": {"hunter": "0.05"},
                "estimated_round_trip_fee_bps": "200",
            }
        }
    )
    held = [
        HeldPositionEdge(
            symbol="OLD",
            notional=Decimal("100000"),
            expected_remaining_edge=Decimal("0.10"),
            metadata={"side": "long", "asset_class": "equity"},
        )
    ]
    opp = StrategyOpportunity(
        strategy_name="volume_flow",
        symbol="NEW",
        side="long",
        created_at=datetime.now(timezone.utc),
        expected_edge=Decimal("0.50"),
        confidence=Decimal("0.50"),
        capital_required=Decimal("25000"),
        expected_holding_hours=6,
        liquidity_score=Decimal("0.8"),
        execution_score=Decimal("0.8"),
        regime_fit_score=Decimal("0.8"),
        risk_cost_score=Decimal("0.05"),
        priority_score=Decimal("0.12"),
        metadata={"asset_class": "equity"},
    )

    assert coord.propose_rotation_actions(held, [opp], active_mode="hunter") == []


def test_hunter_rotation_records_and_respects_symbol_cooldown() -> None:
    coord = GlobalEdgeCoordinator(
        {
            "rotation": {
                "enabled": True,
                "max_replacements_per_tick": {"hunter": 1},
                "min_edge_advantage": {"hunter": "0.01"},
                "estimated_round_trip_fee_bps": "20",
                "symbol_cooldown_sec": 900,
                "min_hold_sec": 900,
            }
        }
    )
    held = [
        HeldPositionEdge(
            symbol="OLD",
            notional=Decimal("100000"),
            expected_remaining_edge=Decimal("0.05"),
            metadata={"side": "long", "asset_class": "equity"},
        )
    ]
    opp = StrategyOpportunity(
        strategy_name="volume_flow",
        symbol="NEW",
        side="long",
        created_at=datetime.now(timezone.utc),
        expected_edge=Decimal("0.80"),
        confidence=Decimal("0.80"),
        capital_required=Decimal("25000"),
        expected_holding_hours=6,
        liquidity_score=Decimal("0.8"),
        execution_score=Decimal("0.8"),
        regime_fit_score=Decimal("0.8"),
        risk_cost_score=Decimal("0.05"),
        priority_score=Decimal("0.20"),
        metadata={"asset_class": "equity"},
    )
    ctx = ReplacementContext()

    actions = coord.propose_rotation_actions(held, [opp], active_mode="hunter", replacement_context=ctx)
    assert [a.kind for a in actions] == ["trim_symbol", "open_strategy"]
    assert ctx.recent_events[-1]["old"] == "OLD"
    assert ctx.recent_events[-1]["new"] == "NEW"
    assert "OLD" in ctx.last_event_at_by_symbol
    assert "NEW" in ctx.last_event_at_by_symbol

    assert coord.propose_rotation_actions(held, [opp], active_mode="hunter", replacement_context=ctx) == []


def test_capital_recycle_respects_symbol_cooldown() -> None:
    coord = GlobalEdgeCoordinator(
        {
            "capital_recycle": {
                "enabled": True,
                "dead_edge_floor": "0.10",
                "max_actions_per_tick": 3,
                "symbol_cooldown_sec": 900,
            }
        }
    )
    held = [
        HeldPositionEdge(
            symbol="STALE",
            notional=Decimal("10000"),
            expected_remaining_edge=Decimal("0.01"),
            metadata={"asset_class": "equity"},
        )
    ]
    ctx = ReplacementContext(
        last_event_at_by_symbol={"STALE": datetime.now(timezone.utc) - timedelta(seconds=30)}
    )

    assert coord.propose_capital_recycle_actions(held, replacement_context=ctx) == []


def test_idle_loss_recycle_closes_weakest_losing_holding() -> None:
    coord = GlobalEdgeCoordinator(
        {
            "capital_recycle": {
                "enabled": True,
                "idle_loss_recycle_enabled": True,
                "idle_loss_max_actions_per_tick": 1,
                "symbol_cooldown_sec": 900,
            },
            "rotation": {
                "estimated_round_trip_fee_bps": "40",
                "fee_edge_multiplier": "1.5",
            },
        }
    )
    held = [
        HeldPositionEdge(
            symbol="BETTER_LOSER",
            notional=Decimal("10000"),
            expected_remaining_edge=Decimal("0.04"),
            broker="ibkr",
            metadata={"asset_class": "equity", "unrealised_return": "-0.01"},
        ),
        HeldPositionEdge(
            symbol="WEAKEST_LOSER",
            notional=Decimal("8000"),
            expected_remaining_edge=Decimal("0.001"),
            broker="ibkr",
            metadata={"asset_class": "equity", "unrealised_return": "-0.001"},
        ),
        HeldPositionEdge(
            symbol="WINNER",
            notional=Decimal("12000"),
            expected_remaining_edge=Decimal("0.00"),
            broker="ibkr",
            metadata={"asset_class": "equity", "unrealised_return": "0.001"},
        ),
    ]

    actions = coord.propose_idle_loss_recycle_actions(
        held,
        replacement_evidence={
            "symbol": "SUCCESSOR",
            "strategy": "trend_following",
            "asset_class": "equity",
            "expected_return": "0.05",
        },
    )

    assert len(actions) == 1
    assert actions[0].symbol == "WEAKEST_LOSER"
    assert actions[0].kind == "trim_symbol"
    assert actions[0].metadata["capital_recycle_reason"] == "idle_loss_recycle"
    assert actions[0].metadata["close_only"] is True
    assert actions[0].metadata["broker"] == "ibkr"
    assert actions[0].metadata["capital_recycle_switching_cost_edge"] == "0.0060"
    assert actions[0].metadata["capital_recycle_replacement_symbol"] == "SUCCESSOR"


def test_idle_loss_recycle_requires_positive_learned_replacement() -> None:
    coord = GlobalEdgeCoordinator(
        {
            "capital_recycle": {
                "enabled": True,
                "idle_loss_recycle_enabled": True,
            }
        }
    )
    held = [
        HeldPositionEdge(
            symbol="LOSER",
            notional=Decimal("10000"),
            expected_remaining_edge=Decimal("0"),
            metadata={"asset_class": "crypto", "unrealised_return": "-0.01"},
        )
    ]

    assert coord.propose_idle_loss_recycle_actions(held) == []
    assert coord.propose_idle_loss_recycle_actions(
        held,
        replacement_evidence={
            "symbol": "NEW",
            "strategy": "mean_reversion",
            "asset_class": "crypto",
            "expected_return": "-0.001",
        },
    ) == []


def test_idle_loss_recycle_keeps_loser_whose_edge_exceeds_switching_cost() -> None:
    coord = GlobalEdgeCoordinator(
        {
            "capital_recycle": {
                "enabled": True,
                "idle_loss_recycle_enabled": True,
            },
            "rotation": {
                "estimated_round_trip_fee_bps": "40",
                "fee_edge_multiplier": "1.5",
            },
        }
    )
    held = [
        HeldPositionEdge(
            symbol="NOISE_LOSS",
            notional=Decimal("10000"),
            expected_remaining_edge=Decimal("0.03"),
            metadata={"asset_class": "crypto", "unrealised_return": "-0.001"},
        )
    ]

    assert coord.propose_idle_loss_recycle_actions(
        held,
        replacement_evidence={
            "symbol": "SUCCESSOR",
            "strategy": "trend_following",
            "asset_class": "crypto",
            "expected_return": "0.035",
        },
    ) == []


def test_idle_loss_recycle_respects_symbol_cooldown() -> None:
    coord = GlobalEdgeCoordinator(
        {
            "capital_recycle": {
                "enabled": True,
                "idle_loss_recycle_enabled": True,
                "symbol_cooldown_sec": 900,
            }
        }
    )
    held = [
        HeldPositionEdge(
            symbol="COOLDOWN",
            notional=Decimal("10000"),
            expected_remaining_edge=Decimal("0.00"),
            metadata={"asset_class": "equity", "unrealised_return": "-0.01"},
        )
    ]
    ctx = ReplacementContext(
        last_event_at_by_symbol={"COOLDOWN": datetime.now(timezone.utc) - timedelta(seconds=30)}
    )

    assert coord.propose_idle_loss_recycle_actions(
        held,
        replacement_context=ctx,
        replacement_evidence={
            "symbol": "SUCCESSOR",
            "strategy": "trend_following",
            "asset_class": "equity",
            "expected_return": "0.05",
        },
    ) == []


# ── D231 — idle_loss_recycle min-hold gating ────────────────────────────────
def _idle_loss_coord() -> GlobalEdgeCoordinator:
    return GlobalEdgeCoordinator(
        {
            "capital_recycle": {
                "enabled": True,
                "idle_loss_recycle_enabled": True,
                "idle_loss_max_actions_per_tick": 1,
                "symbol_cooldown_sec": 900,
            },
        }
    )


def _idle_loss_held(unrealised_return: str = "-0.01") -> list[HeldPositionEdge]:
    return [
        HeldPositionEdge(
            symbol="YOUNG_LOSER",
            notional=Decimal("10000"),
            expected_remaining_edge=Decimal("0.00"),
            broker="ibkr",
            metadata={"asset_class": "equity", "unrealised_return": unrealised_return},
        )
    ]


_IDLE_LOSS_REPLACEMENT = {
    "symbol": "SUCCESSOR",
    "strategy": "trend_following",
    "asset_class": "equity",
    "expected_return": "0.05",
}


def test_idle_loss_recycle_blocks_position_younger_than_min_hold() -> None:
    from risk.protective_exit_gate import ProtectiveExitConfig

    coord = _idle_loss_coord()
    pe_cfg = ProtectiveExitConfig(enabled=True, min_hold_sec=Decimal("259200"))

    actions = coord.propose_idle_loss_recycle_actions(
        _idle_loss_held(),
        replacement_evidence=_IDLE_LOSS_REPLACEMENT,
        position_ages={"ibkr:YOUNG_LOSER": Decimal("1500")},  # 25 minutes old
        protective_exit_config=pe_cfg,
    )

    assert actions == []


def test_idle_loss_recycle_allows_position_past_min_hold() -> None:
    from risk.protective_exit_gate import ProtectiveExitConfig

    coord = _idle_loss_coord()
    pe_cfg = ProtectiveExitConfig(enabled=True, min_hold_sec=Decimal("259200"))

    actions = coord.propose_idle_loss_recycle_actions(
        _idle_loss_held(),
        replacement_evidence=_IDLE_LOSS_REPLACEMENT,
        position_ages={"ibkr:YOUNG_LOSER": Decimal("300000")},  # > 3 days
        protective_exit_config=pe_cfg,
    )

    assert len(actions) == 1
    assert actions[0].symbol == "YOUNG_LOSER"


def test_idle_loss_recycle_allows_unknown_age_when_gate_enabled() -> None:
    """Missing evidence never suppresses (matches protective_exit_gate's own rule)."""
    from risk.protective_exit_gate import ProtectiveExitConfig

    coord = _idle_loss_coord()
    pe_cfg = ProtectiveExitConfig(enabled=True, min_hold_sec=Decimal("259200"))

    actions = coord.propose_idle_loss_recycle_actions(
        _idle_loss_held(),
        replacement_evidence=_IDLE_LOSS_REPLACEMENT,
        position_ages={},  # no fills-derived age known for this key
        protective_exit_config=pe_cfg,
    )

    assert len(actions) == 1


def test_idle_loss_recycle_catastrophic_loss_bypasses_young_gate() -> None:
    """A position down >= catastrophic_loss_pct_position can still be shed young."""
    from risk.protective_exit_gate import ProtectiveExitConfig

    coord = _idle_loss_coord()
    pe_cfg = ProtectiveExitConfig(
        enabled=True,
        min_hold_sec=Decimal("259200"),
        catastrophic_loss_pct_position=Decimal("0.30"),
    )

    actions = coord.propose_idle_loss_recycle_actions(
        _idle_loss_held(unrealised_return="-0.35"),
        replacement_evidence=_IDLE_LOSS_REPLACEMENT,
        position_ages={"ibkr:YOUNG_LOSER": Decimal("1500")},
        protective_exit_config=pe_cfg,
    )

    assert len(actions) == 1


def test_idle_loss_recycle_omitted_gate_args_preserve_pre_d231_behaviour() -> None:
    """Callers that don't pass the new kwargs get the old (unprotected) behaviour."""
    coord = _idle_loss_coord()

    actions = coord.propose_idle_loss_recycle_actions(
        _idle_loss_held(),
        replacement_evidence=_IDLE_LOSS_REPLACEMENT,
    )

    assert len(actions) == 1


def test_shed_respects_symbol_cooldown() -> None:
    coord = GlobalEdgeCoordinator({"shed": {"symbol_cooldown_sec": 900}})
    held = [
        HeldPositionEdge(
            symbol="HOT",
            notional=Decimal("10000"),
            expected_remaining_edge=Decimal("0.01"),
            metadata={"asset_class": "equity"},
        )
    ]
    ctx = ReplacementContext(
        last_event_at_by_symbol={"HOT": datetime.now(timezone.utc) - timedelta(seconds=30)}
    )

    assert coord.propose_shed_actions(
        held,
        cash_target_absolute=Decimal("0"),
        replacement_context=ctx,
    ) == []


def test_max_actions_per_tick_scalar_is_mode_blind_backcompat() -> None:
    """Legacy scalar config: same cap for every mode (v1 behaviour)."""
    cfg = {
        "edge_advantage": {"hunter": "0.01", "trader": "0.01", "defender": "0.01"},
        "max_actions_per_tick": 2,
        "max_notional_fraction_per_action": "1.0",
    }
    coord = GlobalEdgeCoordinator(cfg)
    new_opps = [_opp(f"S{i}", str(Decimal("0.3") - Decimal(f"0.0{i}"))) for i in range(10)]
    for mode in ("hunter", "trader", "defender"):
        actions = coord.propose_actions([], list(new_opps), active_mode=mode)
        assert len(actions) == 2, f"mode={mode} broke legacy scalar cap"


def test_max_actions_per_tick_mode_aware_hunter_opens_many() -> None:
    """Hunter must emit many actions per cycle; defender at most one."""
    cfg = {
        "edge_advantage": {"hunter": "0.01", "trader": "0.02", "defender": "0.12"},
        "max_actions_per_tick": {"hunter": 8, "trader": 3, "defender": 1},
        "max_notional_fraction_per_action": "1.0",
    }
    coord = GlobalEdgeCoordinator(cfg)
    new_opps = [_opp(f"S{i}", str(Decimal("0.3") - Decimal(f"0.0{i}"))) for i in range(12)]

    hunter_actions = coord.propose_actions([], list(new_opps), active_mode="hunter")
    trader_actions = coord.propose_actions([], list(new_opps), active_mode="trader")
    defender_actions = coord.propose_actions([], list(new_opps), active_mode="defender")

    assert len(hunter_actions) == 8, f"hunter got {len(hunter_actions)} actions"
    assert len(trader_actions) == 3, f"trader got {len(trader_actions)} actions"
    assert len(defender_actions) == 1, f"defender got {len(defender_actions)} actions"


def test_max_actions_per_tick_unknown_mode_falls_back_to_trader() -> None:
    cfg = {
        "edge_advantage": {"trader": "0.01"},
        "max_actions_per_tick": {"hunter": 8, "trader": 3, "defender": 1},
        "max_notional_fraction_per_action": "1.0",
    }
    coord = GlobalEdgeCoordinator(cfg)
    new_opps = [_opp(f"S{i}", str(Decimal("0.3") - Decimal(f"0.0{i}"))) for i in range(10)]
    actions = coord.propose_actions([], list(new_opps), active_mode="surge")  # unknown
    assert len(actions) == 3


def test_max_actions_per_tick_invalid_value_defaults_to_3() -> None:
    cfg = {
        "edge_advantage": {"trader": "0.01"},
        "max_actions_per_tick": {"hunter": "banana", "trader": "3"},
        "max_notional_fraction_per_action": "1.0",
    }
    coord = GlobalEdgeCoordinator(cfg)
    new_opps = [_opp(f"S{i}", str(Decimal("0.3") - Decimal(f"0.0{i}"))) for i in range(10)]
    actions = coord.propose_actions([], list(new_opps), active_mode="hunter")
    assert len(actions) == 3  # malformed hunter value → default 3


def test_max_actions_per_tick_explicit_kwarg_overrides_config() -> None:
    """``max_actions=`` kwarg must still override the config (public API)."""
    cfg = {
        "edge_advantage": {"hunter": "0.01"},
        "max_actions_per_tick": {"hunter": 8},
        "max_notional_fraction_per_action": "1.0",
    }
    coord = GlobalEdgeCoordinator(cfg)
    new_opps = [_opp(f"S{i}", str(Decimal("0.3") - Decimal(f"0.0{i}"))) for i in range(10)]
    actions = coord.propose_actions([], list(new_opps), active_mode="hunter", max_actions=2)
    assert len(actions) == 2


# -----------------------------------------------------------------------------
# D030: per-mode ``max_notional_fraction_per_action`` (sleeping-hunter fix)
# -----------------------------------------------------------------------------


def test_notional_fraction_scalar_is_mode_blind_backcompat() -> None:
    """Legacy scalar config: same fraction applied to every mode (pre-D030)."""
    cfg = {
        "edge_advantage": {"hunter": "0.01", "trader": "0.01", "defender": "0.01"},
        "max_actions_per_tick": 1,
        "max_notional_fraction_per_action": "0.25",
    }
    coord = GlobalEdgeCoordinator(cfg)
    opp = _opp("BBB", "0.50", cap="10000")
    for mode in ("hunter", "trader", "defender"):
        actions = coord.propose_actions([], [opp], active_mode=mode)
        assert len(actions) == 1
        assert actions[0].capital == Decimal("2500"), f"mode={mode} scalar broke"


def test_notional_fraction_mode_aware_hunter_gets_full_request() -> None:
    """D030: hunter deploys the full strategy-requested capital; defender trims."""
    cfg = {
        "edge_advantage": {"hunter": "0.01", "trader": "0.01", "defender": "0.01"},
        "max_actions_per_tick": 1,
        "max_notional_fraction_per_action": {
            "hunter": "1.00",
            "trader": "0.50",
            "defender": "0.15",
        },
    }
    coord = GlobalEdgeCoordinator(cfg)
    opp = _opp("BBB", "0.50", cap="10000")

    hunter_a = coord.propose_actions([], [opp], active_mode="hunter")
    trader_a = coord.propose_actions([], [opp], active_mode="trader")
    defender_a = coord.propose_actions([], [opp], active_mode="defender")

    assert hunter_a[0].capital == Decimal("10000"), "hunter must deploy full request"
    assert trader_a[0].capital == Decimal("5000"), "trader scales to half"
    assert defender_a[0].capital == Decimal("1500"), "defender trims to 15%"


def test_notional_fraction_clamps_above_one() -> None:
    """``min(1, frac)`` clamp prevents an accidental >100% blow-up."""
    cfg = {
        "edge_advantage": {"hunter": "0.01"},
        "max_actions_per_tick": 1,
        "max_notional_fraction_per_action": {"hunter": "1.50"},
    }
    coord = GlobalEdgeCoordinator(cfg)
    opp = _opp("BBB", "0.50", cap="10000")
    actions = coord.propose_actions([], [opp], active_mode="hunter")
    assert actions[0].capital == Decimal("10000"), "must not exceed capital_required"


def test_notional_fraction_unknown_mode_falls_back_to_trader() -> None:
    cfg = {
        "edge_advantage": {"trader": "0.01"},
        "max_actions_per_tick": 1,
        "max_notional_fraction_per_action": {
            "hunter": "1.00",
            "trader": "0.40",
            "defender": "0.10",
        },
    }
    coord = GlobalEdgeCoordinator(cfg)
    opp = _opp("BBB", "0.50", cap="10000")
    actions = coord.propose_actions([], [opp], active_mode="surge")
    assert actions[0].capital == Decimal("4000"), "unknown mode must fall back to trader"


def test_notional_fraction_malformed_value_defaults_to_015() -> None:
    cfg = {
        "edge_advantage": {"hunter": "0.01"},
        "max_actions_per_tick": 1,
        "max_notional_fraction_per_action": {"hunter": "banana"},
    }
    coord = GlobalEdgeCoordinator(cfg)
    opp = _opp("BBB", "0.50", cap="10000")
    actions = coord.propose_actions([], [opp], active_mode="hunter")
    assert actions[0].capital == Decimal("1500"), "malformed → 0.15 default"


def test_notional_fraction_default_when_missing_is_015() -> None:
    """No config key at all → conservative 0.15 baseline."""
    cfg = {
        "edge_advantage": {"hunter": "0.01"},
        "max_actions_per_tick": 1,
    }
    coord = GlobalEdgeCoordinator(cfg)
    opp = _opp("BBB", "0.50", cap="10000")
    actions = coord.propose_actions([], [opp], active_mode="hunter")
    assert actions[0].capital == Decimal("1500")


# -----------------------------------------------------------------------------
# D031: respect strategy-proposed sizing (end of over-sizing bug)
# -----------------------------------------------------------------------------


def test_d031_respects_target_notional_over_nav_fallback() -> None:
    """Signal's ``target_notional`` must be honoured verbatim when no override is set."""
    cand = _FakeSignalCandidate(
        "COHR",
        asset_class="equity",
        metadata={"target_notional": "7913.22"},
    )
    opp = signal_candidate_to_strategy_opportunity(
        cand,
        nav=Decimal("1000000"),
        position_pct=Decimal("0.05"),
        price=Decimal("355"),
        max_position_pct=Decimal("0.10"),
    )
    assert opp is not None
    assert opp.capital_required == Decimal("7913.22")
    assert opp.metadata["sizing_source"] == "target_notional"
    assert opp.metadata["sizing_clipped"] is False
    assert opp.metadata["sizing_final_capital_required"] == "7913.22"


def test_d031_respects_risk_notional_override_over_target() -> None:
    """``risk_notional_override`` wins over ``target_notional`` (more specific)."""
    cand = _FakeSignalCandidate(
        "FCOM",
        asset_class="equity",
        metadata={
            "target_notional": "5000",
            "risk_notional_override": "750",
        },
    )
    opp = signal_candidate_to_strategy_opportunity(
        cand,
        nav=Decimal("1000000"),
        position_pct=Decimal("0.05"),
        price=Decimal("74.18"),
        max_position_pct=Decimal("0.10"),
    )
    assert opp is not None
    assert opp.capital_required == Decimal("750")
    assert opp.metadata["sizing_source"] == "risk_notional_override"
    assert opp.metadata["sizing_clipped"] is False


def test_d031_hard_cap_clips_absurd_strategy_request() -> None:
    """Strategy asking for a stupid size must be clipped at ``nav * max_position_pct``."""
    cand = _FakeSignalCandidate(
        "XYZ",
        asset_class="equity",
        metadata={"target_notional": "500000"},  # 50% of NAV
    )
    opp = signal_candidate_to_strategy_opportunity(
        cand,
        nav=Decimal("1000000"),
        position_pct=Decimal("0.05"),
        price=Decimal("100"),
        max_position_pct=Decimal("0.10"),
    )
    assert opp is not None
    assert opp.capital_required == Decimal("100000")  # capped at 10% NAV
    assert opp.metadata["sizing_clipped"] is True
    assert "nav*0.10" in opp.metadata["sizing_clip_reason"]


def test_d031_nav_fallback_when_no_sizing_metadata() -> None:
    """Legacy signals without sizing metadata fall back to ``nav * position_pct``."""
    cand = _FakeSignalCandidate("ABC", asset_class="equity", metadata={})
    opp = signal_candidate_to_strategy_opportunity(
        cand,
        nav=Decimal("1000000"),
        position_pct=Decimal("0.05"),
        price=Decimal("50"),
        max_position_pct=Decimal("0.10"),
    )
    assert opp is not None
    assert opp.capital_required == Decimal("50000")
    assert opp.metadata["sizing_source"] == "nav_fallback"
    assert opp.metadata["sizing_clipped"] is False


def test_d031_does_not_inflate_small_strategy_request_to_nav_fallback() -> None:
    """The hard cap is a ceiling only — small requests stay small."""
    cand = _FakeSignalCandidate(
        "TINY",
        asset_class="equity",
        metadata={"target_notional": "500"},
    )
    opp = signal_candidate_to_strategy_opportunity(
        cand,
        nav=Decimal("1000000"),
        position_pct=Decimal("0.05"),  # fallback would be 50k
        price=Decimal("10"),
        max_position_pct=Decimal("0.10"),
    )
    assert opp is not None
    assert opp.capital_required == Decimal("500")
    assert opp.metadata["sizing_source"] == "target_notional"


def test_d031_audit_metadata_complete_on_emitted_action() -> None:
    """CoordinatorAction must carry the full sizing audit trail through to RawSignal."""
    cand = _FakeSignalCandidate(
        "COHR",
        asset_class="equity",
        metadata={"target_notional": "7913.22"},
    )
    opp = signal_candidate_to_strategy_opportunity(
        cand,
        nav=Decimal("1000000"),
        position_pct=Decimal("0.05"),
        price=Decimal("355"),
        max_position_pct=Decimal("0.10"),
    )
    assert opp is not None

    cfg = {
        "edge_advantage": {"hunter": "0.01"},
        "max_actions_per_tick": 1,
        "max_notional_fraction_per_action": {"hunter": "1.00"},
    }
    coord = GlobalEdgeCoordinator(cfg)
    actions = coord.propose_actions([], [opp], active_mode="hunter")
    assert len(actions) == 1
    md = actions[0].metadata
    for key in (
        "sizing_source",
        "sizing_proposed_base_notional",
        "sizing_hard_cap_notional",
        "sizing_final_capital_required",
        "sizing_clipped",
        "sizing_pre_mode_capital",
        "sizing_mode",
        "sizing_mode_fraction",
        "sizing_final_action_capital",
    ):
        assert key in md, f"missing audit field: {key}"
    assert md["sizing_source"] == "target_notional"
    assert md["sizing_mode"] == "hunter"
    assert Decimal(md["sizing_mode_fraction"]) == Decimal("1")
    assert actions[0].capital == Decimal("7913.22")
    assert md["allocation_selected"] is True
    assert Decimal(str(md["confidence"])) == opp.confidence
    assert Decimal(str(md["expected_edge"])) == opp.expected_edge


def test_d031_held_position_oversized_flag_when_above_ceiling() -> None:
    """Held position > nav*max_position_pct*1.25 must carry oversized flag."""
    from portfolio.global_edge_coordinator import held_positions_from_portfolio

    # COHR at £160k on £1M NAV = 16 % gross, ratio = 1.60 (above 1.25 flag)
    # TINY at £50 on £1M = 0.000005 % gross, ratio well under 1.0
    portfolio = {
        "positions": {
            "COHR": {"quantity": "500", "current_price": "320", "broker": "ibkr"},
            "TINY": {"quantity": "10", "current_price": "5.00", "broker": "alpaca"},
        }
    }
    nav = Decimal("1000000")
    held = held_positions_from_portfolio(
        portfolio,
        nav=nav,
        max_position_pct=Decimal("0.10"),
        oversize_flag_ratio=Decimal("1.25"),
    )
    by_sym = {h.symbol: h for h in held}
    assert "COHR" in by_sym and "TINY" in by_sym
    assert by_sym["COHR"].metadata.get("oversized_position_flag") is True
    assert Decimal(by_sym["COHR"].metadata["position_above_target_ratio"]) > Decimal("1.25")
    assert by_sym["TINY"].metadata.get("oversized_position_flag") is False
    assert by_sym["COHR"].expected_remaining_edge < by_sym["TINY"].expected_remaining_edge


def test_oversized_held_position_is_trimmed_before_small_position() -> None:
    """Replacement ranking should not keep oversized IBKR holdings behind small first-loaded rows."""
    portfolio = {
        "positions": {
            "SMALL": {"quantity": "10", "current_price": "20", "broker": "alpaca"},
            "AXTA": {"quantity": "54861", "current_price": "29.67", "broker": "ibkr"},
        }
    }
    held = held_positions_from_portfolio(
        portfolio,
        nav=Decimal("1100000"),
        max_position_pct=Decimal("0.10"),
    )
    opp = _opp("BAMB", "0.65", "6500")
    coord = GlobalEdgeCoordinator({"replacement_threshold": {"hunter": "0.01"}, "max_actions": {"hunter": 2}})
    actions = coord.propose_actions(held, [opp], active_mode="hunter")
    assert actions
    assert actions[0].kind == "trim_symbol"
    assert actions[0].symbol == "AXTA"


def test_d031_arbitrage_path_capital_unchanged() -> None:
    """Arbitrage opportunity builders must NOT be affected by D031."""
    from portfolio.global_edge_coordinator import cross_exchange_dict_to_strategy_opportunity

    d = {
        "symbol": "BTC-USDT",
        "side": "ARBITRAGE_SPOT_SPREAD",
        "confidence": "0.8",
        "metadata": {"net_spread": "100"},
    }
    opp = cross_exchange_dict_to_strategy_opportunity(
        d, capital=Decimal("5000"), edge_boost=Decimal("0.015")
    )
    assert opp.capital_required == Decimal("5000")  # unchanged
    # Arb path does not populate D031 sizing audit keys; that's fine — the
    # execution boundary guard exempts arbitrage sides explicitly.
    assert "sizing_source" not in opp.metadata


def test_propose_actions_adaptive_concentration_clamping() -> None:
    cfg = {
        "emit_trim_actions": True,
        "adaptive": {
            "softmax_lambda": 5.0,
            "target_tolerance_pct": "0.0025",
        },
        "minimum_order_sizes_usd": {
            "equity": "500",
        },
        "max_actions_per_tick": 3,
        "max_notional_fraction_per_action": "1.0",
    }
    coord = GlobalEdgeCoordinator(cfg)
    held = [
        HeldPositionEdge(
            symbol="AAA",
            notional=Decimal("4600"),
            expected_remaining_edge=Decimal("0.10"),
            metadata={"side": "long", "asset_class": "equity"},
        ),
        HeldPositionEdge(
            symbol="BBB",
            notional=Decimal("1000"),
            expected_remaining_edge=Decimal("0.12"),
            metadata={"side": "long", "asset_class": "equity"},
        )
    ]
    new_opps = [
        StrategyOpportunity(
            strategy_name="momentum_breakout",
            symbol="AAA",
            side="long",
            created_at=datetime.now(timezone.utc),
            expected_edge=Decimal("0.80"),
            confidence=Decimal("0.80"),
            capital_required=Decimal("5000"),
            expected_holding_hours=24,
            liquidity_score=Decimal("0.8"),
            execution_score=Decimal("0.8"),
            regime_fit_score=Decimal("0.8"),
            risk_cost_score=Decimal("0.05"),
            priority_score=Decimal("0.80"),
            metadata={"asset_class": "equity"},
        ),
        StrategyOpportunity(
            strategy_name="momentum_breakout",
            symbol="CCC",
            side="long",
            created_at=datetime.now(timezone.utc),
            expected_edge=Decimal("0.70"),
            confidence=Decimal("0.70"),
            capital_required=Decimal("5000"),
            expected_holding_hours=24,
            liquidity_score=Decimal("0.8"),
            execution_score=Decimal("0.8"),
            regime_fit_score=Decimal("0.8"),
            risk_cost_score=Decimal("0.05"),
            priority_score=Decimal("0.70"),
            metadata={"asset_class": "equity"},
        )
    ]
    actions = coord.propose_actions(
        held,
        new_opps,
        active_mode="trader",
        gross_target_capital=Decimal("5000"),
        concentration_exponent=Decimal("1.0"),
        max_position_notional=Decimal("5000"),
    )
    assert len(actions) == 1
    assert actions[0].symbol == "CCC"
    assert actions[0].capital == Decimal("4950.00")
