"""D118 — Tests for the online weight learner."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from data.universe_prefilter import COMPONENT_NAMES, PriorityBreakdown, uniform_weights
from data.universe_weight_learner import (
    MAX_HISTORY_KEPT,
    TrainingRow,
    WEIGHT_CEILING,
    WEIGHT_FLOOR,
    WeightLearner,
    WeightLearnerState,
    build_training_rows,
    load_weight_learner_state,
    save_weight_learner_state,
)


def _utc(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day, tzinfo=timezone.utc)


def _row(symbol: str, **components: float) -> TrainingRow:
    full = {name: 0.0 for name in COMPONENT_NAMES}
    full.update(components)
    return TrainingRow(symbol=symbol, components=full, label=int(components.pop("__label__", 0)))


def _row_with_label(symbol: str, label: int, **components: float) -> TrainingRow:
    full = {name: 0.0 for name in COMPONENT_NAMES}
    full.update(components)
    return TrainingRow(symbol=symbol, components=full, label=int(label))


# ---------------------------------------------------------------------------
# 1. Bootstrap is uniform
# ---------------------------------------------------------------------------


def test_default_state_is_uniform():
    state = WeightLearnerState()
    assert state.cycle_count == 0
    assert state.weights == uniform_weights()
    assert all(v == 0.0 for v in state.grad_sq.values())


def test_load_missing_file_returns_uniform(tmp_path):
    state = load_weight_learner_state(tmp_path / "weights.json")
    assert state.weights == uniform_weights()


def test_load_corrupt_file_returns_uniform(tmp_path):
    p = tmp_path / "weights.json"
    p.write_text("nope", encoding="utf-8")
    state = load_weight_learner_state(p)
    assert state.weights == uniform_weights()


# ---------------------------------------------------------------------------
# 2. Single-step update moves predictive components up
# ---------------------------------------------------------------------------


def test_update_increases_weight_of_perfectly_predictive_component():
    learner = WeightLearner()
    # Build 20 rows where only liquidity_prior carries signal: label=1
    # iff liquidity_prior >= 0.5.
    rows: list[TrainingRow] = []
    for i in range(20):
        liq = (i % 10) / 10.0
        rows.append(_row_with_label(f"S{i}", 1 if liq >= 0.5 else 0, liquidity_prior=liq))
    initial = learner.current_weights()
    new = learner.update(rows, now=_utc(2026, 5, 19))
    # liquidity_prior should now have a clearly higher weight than the
    # other (zero-signal) components.
    other_avg = sum(new[n] for n in COMPONENT_NAMES if n != "liquidity_prior") / 5.0
    assert new["liquidity_prior"] > initial["liquidity_prior"]
    assert new["liquidity_prior"] > other_avg


def test_update_with_empty_rows_returns_current_weights():
    learner = WeightLearner()
    weights = learner.current_weights()
    assert learner.update([]) == weights
    assert learner.cycle_count == 0


# ---------------------------------------------------------------------------
# 3. AdaGrad damping: same gradient applied many times yields shrinking steps
# ---------------------------------------------------------------------------


def test_adagrad_damps_repeated_gradient():
    learner = WeightLearner()
    # Always-positive label with always-positive feature -> repeated
    # positive gradient on anchor_pin only.
    constant_row = _row_with_label("AAPL", 1, anchor_pin=1.0)
    step_sizes: list[float] = []
    prev = learner.current_weights()["anchor_pin"]
    for _ in range(8):
        new = learner.update([constant_row])
        delta = new["anchor_pin"] - prev
        step_sizes.append(delta)
        prev = new["anchor_pin"]
    # Each subsequent step should be smaller (AdaGrad damping).
    assert all(step_sizes[i] >= step_sizes[i + 1] for i in range(len(step_sizes) - 1))


# ---------------------------------------------------------------------------
# 4. Bound clamping at WEIGHT_FLOOR / WEIGHT_CEILING + renormalisation
# ---------------------------------------------------------------------------


def test_weights_stay_within_floor_and_ceiling():
    # Force one component to dominate with a huge positive gradient.
    learner = WeightLearner()
    big_row = _row_with_label("X", 1, anchor_pin=1.0)
    for _ in range(50):
        learner.update([big_row])
    weights = learner.current_weights()
    assert all(WEIGHT_FLOOR - 1e-9 <= w <= WEIGHT_CEILING + 1e-9 for w in weights.values())


def test_weights_always_renormalise_to_one():
    learner = WeightLearner()
    rows = [_row_with_label(f"S{i}", i % 2, liquidity_prior=(i % 3) / 3.0) for i in range(10)]
    for _ in range(5):
        learner.update(rows)
        assert sum(learner.current_weights().values()) == pytest.approx(1.0)


def test_zero_internal_weights_fall_back_to_uniform():
    state = WeightLearnerState(weights={n: 0.0 for n in COMPONENT_NAMES})
    learner = WeightLearner(state=state)
    weights = learner.current_weights()
    expected = uniform_weights()
    for name in COMPONENT_NAMES:
        assert weights[name] == pytest.approx(expected[name])


def test_negative_internal_weight_clamped_to_floor():
    state = WeightLearnerState(weights={n: -1.0 for n in COMPONENT_NAMES})
    state.weights["anchor_pin"] = 5.0  # ceiling-clamped to 0.70
    learner = WeightLearner(state=state)
    w = learner.current_weights()
    # All other components clamped at floor and renormalised; anchor_pin
    # is the ceiling-clamped value after renorm.
    assert all(v >= WEIGHT_FLOOR - 1e-9 for v in w.values())
    assert w["anchor_pin"] >= w["liquidity_prior"]


# ---------------------------------------------------------------------------
# 5. EWMA decay shrinks the AdaGrad accumulator over cycles
# ---------------------------------------------------------------------------


def test_ewma_decay_reduces_old_grad_sq():
    learner = WeightLearner(ewma_cycles=2.0)
    row = _row_with_label("X", 1, anchor_pin=1.0)
    learner.update([row])
    g1 = learner.state.grad_sq["anchor_pin"]
    learner.update([])  # empty cycle -> no decay (early return)
    # Force a real cycle but with a different feature so anchor_pin
    # accumulator only decays.
    learner.update([_row_with_label("X", 0, liquidity_prior=1.0)])
    g2 = learner.state.grad_sq["anchor_pin"]
    assert g2 < g1


# ---------------------------------------------------------------------------
# 6. Cycle count + history
# ---------------------------------------------------------------------------


def test_cycle_count_increments():
    learner = WeightLearner()
    row = _row_with_label("X", 1, anchor_pin=1.0)
    learner.update([row])
    learner.update([row])
    assert learner.cycle_count == 2


def test_history_records_each_update_snapshot():
    learner = WeightLearner()
    row = _row_with_label("X", 1, anchor_pin=1.0)
    learner.update([row], now=_utc(2026, 5, 19))
    learner.update([row], now=_utc(2026, 5, 20))
    assert len(learner.state.history) == 2
    first = learner.state.history[0]
    assert "weights" in first
    assert set(first["weights"].keys()) == set(COMPONENT_NAMES)


def test_history_is_capped():
    learner = WeightLearner()
    row = _row_with_label("X", 1, anchor_pin=1.0)
    for _ in range(MAX_HISTORY_KEPT + 5):
        learner.update([row])
    assert len(learner.state.history) == MAX_HISTORY_KEPT


# ---------------------------------------------------------------------------
# 7. Persistence round-trip
# ---------------------------------------------------------------------------


def test_save_then_load_preserves_state(tmp_path):
    learner = WeightLearner()
    row = _row_with_label("X", 1, anchor_pin=1.0, liquidity_prior=0.3)
    learner.update([row], now=_utc(2026, 5, 19))
    path = tmp_path / "weights.json"
    save_weight_learner_state(learner.state, path=path)
    loaded = load_weight_learner_state(path)
    assert loaded.cycle_count == 1
    assert loaded.last_update_at is not None
    assert sum(loaded.weights.values()) > 0
    assert len(loaded.history) == 1


def test_save_handles_subdir(tmp_path):
    learner = WeightLearner()
    learner.update([_row_with_label("X", 1, anchor_pin=1.0)])
    p = tmp_path / "subdir" / "weights.json"
    save_weight_learner_state(learner.state, path=p)
    assert p.is_file()
    tmp_files = [pp for pp in (tmp_path / "subdir").iterdir() if pp.suffix == ".tmp"]
    assert tmp_files == []


def test_load_ignores_garbage_history(tmp_path):
    p = tmp_path / "weights.json"
    p.write_text(
        json.dumps(
            {
                "version": 1,
                "weights": {name: 1.0 / len(COMPONENT_NAMES) for name in COMPONENT_NAMES},
                "grad_sq": {name: 0.0 for name in COMPONENT_NAMES},
                "cycle_count": 0,
                "history": ["not-a-dict", {"ts": "abc"}, 42],
                "last_update_at": None,
            }
        ),
        encoding="utf-8",
    )
    loaded = load_weight_learner_state(p)
    assert loaded.history == []


# ---------------------------------------------------------------------------
# 8. build_training_rows pairs picks with watching outcomes
# ---------------------------------------------------------------------------


def test_build_training_rows_labels_correctly():
    picks = {
        "AAPL": PriorityBreakdown("AAPL", 0.9, {n: 0.5 for n in COMPONENT_NAMES}),
        "GHOST": PriorityBreakdown("GHOST", 0.1, {n: 0.1 for n in COMPONENT_NAMES}),
    }
    watching = ["AAPL", "TSLA"]  # TSLA wasn't picked
    rows = build_training_rows(picks, watching)
    label_map = {r.symbol: r.label for r in rows}
    assert label_map["AAPL"] == 1
    assert label_map["GHOST"] == 0
    assert "TSLA" not in label_map  # never picked -> never trained


def test_build_training_rows_normalises_symbols():
    picks = {
        "aapl": PriorityBreakdown("AAPL", 0.9, {n: 0.5 for n in COMPONENT_NAMES}),
    }
    rows = build_training_rows(picks, ["AAPL"])
    assert rows[0].label == 1
    assert rows[0].symbol == "AAPL"


def test_build_training_rows_skips_empty():
    picks = {"": PriorityBreakdown("", 0.0, {n: 0.0 for n in COMPONENT_NAMES})}
    assert build_training_rows(picks, []) == []
