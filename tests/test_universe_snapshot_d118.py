"""D118 — Tests for the snapshot service extensions.

Covers the 6-stage funnel, asset-class coverage aggregation, score-age
attachment to symbol rows, and transitions/priority_rule wiring through
``build_universe_snapshot_dict``.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from data.universe_budget_controller import (
    BudgetController,
    CycleObservation,
    save_budget_state,
)
from data.universe_score_ages import ScoreAges, save_score_ages
from data.universe_transitions import (
    TierTransition,
    TransitionBuffer,
    save_transitions,
)
from data.universe_weight_learner import WeightLearner, save_weight_learner_state
from universe.snapshot_service import (
    _asset_class_coverage_block,
    _build_d118_funnel,
    _priority_rule_block,
    _symbols_fallback,
    _score_ages_by_symbol,
    _transitions_block,
    build_universe_snapshot_dict,
)
from universe.universe_tiers import UniverseIntelligenceState


def _utc(year: int, month: int, day: int, hour: int = 0) -> datetime:
    return datetime(year, month, day, hour, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# 1. 6-stage funnel structure
# ---------------------------------------------------------------------------


def test_funnel_has_four_stages_in_d118_order():
    funnel = _build_d118_funnel(
        unique_source_count=16_000,
        priority_ranked_count=400,
        scored_count=400,
        watching_count=300,
        promoted_count=10,
        active_count=80,
        broker_listing_count=31_000,
        drops_eligible=[],
        drops_watching=[],
        budget_block=None,
    )
    stages = [s["stage"] for s in funnel]
    assert stages == [
        "unique_normalized",
        "scored",
        "watching",
        "active_reps",
    ]
    watching = next(s for s in funnel if s["stage"] == "watching")
    assert watching["meta"]["promoted_now"] == 10


def test_funnel_unique_normalized_carries_broker_listings_meta():
    funnel = _build_d118_funnel(
        unique_source_count=16_000,
        priority_ranked_count=400,
        scored_count=400,
        watching_count=300,
        promoted_count=10,
        active_count=80,
        broker_listing_count=31_000,
        drops_eligible=[],
        drops_watching=[],
        budget_block=None,
    )
    first = funnel[0]
    assert first["stage"] == "unique_normalized"
    assert first["meta"]["broker_listings"] == 31_000


def test_funnel_scored_attaches_budget_meta_and_pick_gap():
    budget_block = {
        "target_budget": 400,
        "binding_constraint": "throughput",
        "cycle_count": 5,
    }
    funnel = _build_d118_funnel(
        unique_source_count=16_000,
        priority_ranked_count=400,
        scored_count=395,
        watching_count=300,
        promoted_count=10,
        active_count=80,
        broker_listing_count=31_000,
        drops_eligible=[],
        drops_watching=[],
        drops_scored=[{"reason": "yfinance timeout / no score", "count": 5}],
        budget_block=budget_block,
    )
    scored_stage = next(s for s in funnel if s["stage"] == "scored")
    assert scored_stage["count"] == 395
    assert scored_stage["meta"]["target_budget"] == 400
    assert scored_stage["meta"]["binding_constraint"] == "throughput"
    assert scored_stage["meta"]["budget_attempted"] == 400
    assert scored_stage["meta"]["score_failures"] == 5


def test_d118_scoring_counts_from_last_observation():
    from universe.snapshot_service import _d118_scoring_counts

    ranked, scored, drops = _d118_scoring_counts(
        priority_ranked_fallback=400,
        scored_fallback=400,
        budget_block={
            "target_budget": 500,
            "last_observation": {"budget": 14820, "scored": 14810},
        },
    )
    assert ranked == 14820
    assert scored == 14810
    assert drops == [{"reason": "yfinance timeout / no score", "count": 10}]


# ---------------------------------------------------------------------------
# 2. Asset-class coverage aggregation
# ---------------------------------------------------------------------------


def test_asset_class_coverage_aggregates_by_klass():
    rows = [
        {"sym": "AAPL", "klass": "equity"},
        {"sym": "MSFT", "klass": "equity"},
        {"sym": "BTC-USD", "klass": "crypto"},
        {"sym": "EURUSD=X", "klass": "fx"},
    ]
    coverage = _asset_class_coverage_block(rows)
    assert coverage["total"] == 4
    by_class = {item["klass"]: item for item in coverage["by_asset_class"]}
    assert by_class["equity"]["count"] == 2
    assert by_class["equity"]["share"] == pytest.approx(0.5)
    assert by_class["crypto"]["count"] == 1
    assert by_class["fx"]["count"] == 1
    # Sorted by count desc, then klass asc.
    assert coverage["by_asset_class"][0]["klass"] == "equity"


def test_asset_class_coverage_handles_empty():
    coverage = _asset_class_coverage_block([])
    assert coverage["total"] == 0
    assert coverage["by_asset_class"] == []


def test_symbol_rows_are_not_truncated_below_watch_count():
    syms = [f"SYM{i}" for i in range(784)]
    intel = UniverseIntelligenceState(core=[f"SYM{i}" for i in range(87)])
    rows = _symbols_fallback(
        pipeline_syms=[],
        tier_flat=syms,
        scores={s: float(i) for i, s in enumerate(syms)},
        caps={"core_max": 100, "max_symbols": 784, "scan_max": 684, "candidates": 800},
        cfg={},
        intel_disabled=False,
        intel=intel,
    )
    assert len(rows) == 784
    by_stage: dict[str, int] = {}
    for row in rows:
        by_stage[row["stage"]] = by_stage.get(row["stage"], 0) + 1
    assert by_stage["active_reps"] == 87
    assert by_stage["watching"] == 697


# ---------------------------------------------------------------------------
# 3. Priority-rule block loads from persisted state
# ---------------------------------------------------------------------------


def test_priority_rule_block_returns_none_when_no_state(tmp_path, monkeypatch):
    # Point the loaders at empty directories so no state exists.
    from data import universe_budget_controller, universe_score_ages, universe_weight_learner

    monkeypatch.setattr(
        universe_score_ages, "DEFAULT_SCORE_AGES_PATH", tmp_path / "ages.json"
    )
    monkeypatch.setattr(
        universe_weight_learner, "DEFAULT_WEIGHTS_PATH", tmp_path / "weights.json"
    )
    monkeypatch.setattr(
        universe_budget_controller, "DEFAULT_BUDGET_PATH", tmp_path / "budget.json"
    )
    out = _priority_rule_block()
    assert out is None


def test_priority_rule_block_surfaces_weights_and_budget(tmp_path, monkeypatch):
    from data import (
        universe_budget_controller,
        universe_prefilter,
        universe_score_ages,
        universe_weight_learner,
    )

    ages_path = tmp_path / "ages.json"
    weights_path = tmp_path / "weights.json"
    budget_path = tmp_path / "budget.json"
    monkeypatch.setattr(
        universe_score_ages, "DEFAULT_SCORE_AGES_PATH", ages_path
    )
    monkeypatch.setattr(
        universe_weight_learner, "DEFAULT_WEIGHTS_PATH", weights_path
    )
    monkeypatch.setattr(
        universe_budget_controller, "DEFAULT_BUDGET_PATH", budget_path
    )

    # Seed score ages.
    ages = ScoreAges()
    ages.record_scores({"AAPL": 12.0}, now=_utc(2026, 5, 19))
    save_score_ages(ages, path=ages_path)

    # Seed weights via a real learner update.
    learner = WeightLearner()
    row_components = {name: 0.5 for name in universe_prefilter.COMPONENT_NAMES}
    from data.universe_weight_learner import TrainingRow

    learner.update([TrainingRow("AAPL", row_components, 1)], now=_utc(2026, 5, 19))
    save_weight_learner_state(learner.state, path=weights_path)

    # Seed budget controller.
    ctrl = BudgetController()
    ctrl.observe(
        CycleObservation(
            budget=300,
            scored=300,
            measured_duration_sec=1800.0,
            cycle_interval_sec=3600.0,
            concurrency=10,
            max_watching_rank=120,
        )
    )
    ctrl.compute_next_budget(
        unique_normalized=16_000, cycle_interval_sec=3600, concurrency=10
    )
    save_budget_state(ctrl.state, path=budget_path)

    out = _priority_rule_block()
    assert out is not None
    assert out["enabled"] is True
    # Live weights sum to 1.0 (clamp + renormalise).
    assert sum(out["weights"].values()) == pytest.approx(1.0)
    assert out["weights_cycle_count"] >= 1
    assert out["budget"]["target_budget"] == 300
    assert "score_age_summary" in out
    assert out["score_age_summary"]["total_tracked"] == 1


# ---------------------------------------------------------------------------
# 4. Transitions block returns recent rows
# ---------------------------------------------------------------------------


def test_transitions_block_returns_recent_rows(tmp_path, monkeypatch):
    from data import universe_transitions

    p = tmp_path / "transitions.json"
    monkeypatch.setattr(
        universe_transitions, "DEFAULT_TRANSITIONS_PATH", p
    )
    buf = TransitionBuffer(capacity=5)
    for i in range(7):
        buf.append(
            TierTransition(
                ts=f"t{i}",
                symbol=f"S{i}",
                from_tier="scan",
                to_tier="core",
                reason="promoted_within_watching",
            )
        )
    save_transitions(buf, path=p)
    rows = _transitions_block(limit=10)
    # Ring buffer truncates to last 5 on append; we read all of them.
    assert len(rows) == 5
    assert rows[-1]["symbol"] == "S6"
    assert rows[0]["reason"] == "promoted_within_watching"


def test_transitions_block_empty_when_no_file(tmp_path, monkeypatch):
    from data import universe_transitions

    monkeypatch.setattr(
        universe_transitions, "DEFAULT_TRANSITIONS_PATH", tmp_path / "nope.json"
    )
    assert _transitions_block(limit=10) == []


# ---------------------------------------------------------------------------
# 5. score_ages_by_symbol projection
# ---------------------------------------------------------------------------


def test_score_ages_by_symbol_returns_expected_shape(tmp_path, monkeypatch):
    from data import universe_score_ages

    p = tmp_path / "ages.json"
    monkeypatch.setattr(
        universe_score_ages, "DEFAULT_SCORE_AGES_PATH", p
    )
    ages = ScoreAges()
    ages.record_scores({"AAPL": 12.0}, now=_utc(2026, 5, 19))
    ages.observe_unseen(["MSFT"], now=_utc(2026, 5, 19))
    save_score_ages(ages, path=p)

    by_sym = _score_ages_by_symbol()
    assert "AAPL" in by_sym
    assert "MSFT" in by_sym
    assert by_sym["AAPL"]["last_score"] == 12.0
    assert by_sym["AAPL"]["score_count"] == 1
    assert by_sym["MSFT"]["last_scored_at"] is None
    assert by_sym["MSFT"]["score_count"] == 0


# ---------------------------------------------------------------------------
# 6. build_universe_snapshot_dict end-to-end emits D118 fields
# ---------------------------------------------------------------------------


def test_snapshot_emits_d118_fields(tmp_path, monkeypatch):
    # Wire all D118 state files at the loader level so the snapshot
    # service picks them up via its read-only block helpers.
    from data import (
        universe_budget_controller,
        universe_score_ages,
        universe_transitions,
        universe_weight_learner,
    )

    monkeypatch.setattr(
        universe_score_ages, "DEFAULT_SCORE_AGES_PATH", tmp_path / "ages.json"
    )
    monkeypatch.setattr(
        universe_weight_learner, "DEFAULT_WEIGHTS_PATH", tmp_path / "weights.json"
    )
    monkeypatch.setattr(
        universe_budget_controller, "DEFAULT_BUDGET_PATH", tmp_path / "budget.json"
    )
    monkeypatch.setattr(
        universe_transitions, "DEFAULT_TRANSITIONS_PATH", tmp_path / "transitions.json"
    )

    payload = build_universe_snapshot_dict(
        broker_symbol_totals={"ibkr": 10, "kraken": 5},
        broker_symbols={"ibkr": ["SPY", "AAPL"], "kraken": ["BTC-USD"]},
    )
    # Funnel has 4 D118 stages (promotions are watching metadata, not a stage).
    stages = [s["stage"] for s in payload["funnel"]]
    assert stages == [
        "unique_normalized",
        "scored",
        "watching",
        "active_reps",
    ]
    # New top-level D118 blocks are present.
    assert "transitions" in payload
    assert "asset_class_coverage" in payload
    assert "priority_rule" in payload
    # No persisted state -> priority_rule is None, transitions are [].
    assert payload["priority_rule"] is None
    assert payload["transitions"] == []
