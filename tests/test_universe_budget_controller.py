"""D118 — Tests for the self-tuning scoring-budget controller."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from data.universe_budget_controller import (
    AIMD_DECREASE_FACTOR,
    AIMD_INCREASE_FACTOR,
    BOOTSTRAP_SEC_PER_CALL,
    BUDGET_FLOOR,
    BudgetController,
    BudgetState,
    CycleObservation,
    OVERRUN_THRESHOLD,
    UNDER_UTIL_THRESHOLD,
    UTILITY_WINDOW,
    bootstrap_budget,
    load_budget_state,
    save_budget_state,
)


def _utc(year: int, month: int, day: int, hour: int = 0) -> datetime:
    return datetime(year, month, day, hour, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# 1. Bootstrap
# ---------------------------------------------------------------------------


def test_bootstrap_uses_2sec_per_call_assumption():
    # concurrency=10, interval=3600s -> 10 * 3600 / 2 = 18,000 -> capped at BUDGET_CEILING
    budget = bootstrap_budget(
        unique_normalized=16_000, concurrency=10, cycle_interval_sec=3600
    )
    assert budget == 800


def test_bootstrap_respects_universe_size_cap():
    budget = bootstrap_budget(
        unique_normalized=500, concurrency=10, cycle_interval_sec=3600
    )
    assert budget == 500


def test_bootstrap_zero_universe_returns_zero():
    budget = bootstrap_budget(
        unique_normalized=0, concurrency=10, cycle_interval_sec=3600
    )
    assert budget == 0


def test_bootstrap_respects_floor():
    # concurrency=1, interval=1s -> 1/2 = 0 floor-of-bootstrap
    budget = bootstrap_budget(
        unique_normalized=1000, concurrency=1, cycle_interval_sec=1
    )
    assert budget >= BUDGET_FLOOR


# ---------------------------------------------------------------------------
# 2. compute_next_budget — first call uses bootstrap
# ---------------------------------------------------------------------------


def test_first_call_returns_bootstrap_and_marks_binding():
    ctrl = BudgetController()
    budget = ctrl.compute_next_budget(
        unique_normalized=16_000, cycle_interval_sec=3600, concurrency=10
    )
    assert budget > BUDGET_FLOOR
    assert ctrl.state.binding_constraint == "bootstrap"


def test_first_call_with_empty_universe_returns_zero():
    ctrl = BudgetController()
    budget = ctrl.compute_next_budget(
        unique_normalized=0, cycle_interval_sec=3600, concurrency=10
    )
    assert budget == 0


# ---------------------------------------------------------------------------
# 3. AIMD — under-utilisation grows, overrun shrinks, stable holds
# ---------------------------------------------------------------------------


def test_under_utilisation_grows_budget():
    ctrl = BudgetController()
    ctrl.observe(
        CycleObservation(
            budget=200,
            scored=200,
            measured_duration_sec=100.0,  # 100/3600 ~ 0.028 utilisation
            cycle_interval_sec=3600.0,
            concurrency=10,
            max_watching_rank=None,
        )
    )
    next_budget = ctrl.compute_next_budget(
        unique_normalized=16_000, cycle_interval_sec=3600, concurrency=10
    )
    assert next_budget > 200
    assert next_budget <= int(200 * AIMD_INCREASE_FACTOR) + 1
    assert ctrl.state.binding_constraint == "throughput"


def test_overrun_shrinks_budget():
    ctrl = BudgetController()
    ctrl.observe(
        CycleObservation(
            budget=400,
            scored=400,
            measured_duration_sec=4000.0,  # utilisation > 1.0
            cycle_interval_sec=3600.0,
            concurrency=10,
            max_watching_rank=None,
        )
    )
    next_budget = ctrl.compute_next_budget(
        unique_normalized=16_000, cycle_interval_sec=3600, concurrency=10
    )
    assert next_budget < 400
    assert next_budget >= int(400 * AIMD_DECREASE_FACTOR) - 1


def test_stable_utilisation_holds_budget():
    ctrl = BudgetController()
    ctrl.observe(
        CycleObservation(
            budget=300,
            scored=300,
            measured_duration_sec=3200.0,  # 0.88 utilisation (in stable band)
            cycle_interval_sec=3600.0,
            concurrency=10,
            max_watching_rank=None,
        )
    )
    next_budget = ctrl.compute_next_budget(
        unique_normalized=16_000, cycle_interval_sec=3600, concurrency=10
    )
    assert next_budget == 300
    assert ctrl.state.binding_constraint == "stable"


# ---------------------------------------------------------------------------
# 4. Utility saturation
# ---------------------------------------------------------------------------


def test_utility_constraint_binds_when_lower_than_throughput():
    ctrl = BudgetController()
    # Five cycles with watching tier always reaching only rank 100.
    for i in range(5):
        ctrl.observe(
            CycleObservation(
                budget=400,
                scored=400,
                measured_duration_sec=100.0,  # super under-utilised
                cycle_interval_sec=3600.0,
                concurrency=10,
                max_watching_rank=100,
            )
        )
    next_budget = ctrl.compute_next_budget(
        unique_normalized=16_000, cycle_interval_sec=3600, concurrency=10
    )
    # Throughput says grow; utility says we only need 100 + buffer (floor 200).
    assert next_budget == BUDGET_FLOOR
    assert ctrl.state.binding_constraint == "utility"


def test_utility_buffer_grows_with_rank_variance():
    ctrl = BudgetController()
    # Variable depth: rank fluctuates between 80 and 200.
    ranks = [80, 200, 100, 180, 120, 190, 90, 170, 110, 160]
    for r in ranks:
        ctrl.observe(
            CycleObservation(
                budget=400,
                scored=400,
                measured_duration_sec=100.0,
                cycle_interval_sec=3600.0,
                concurrency=10,
                max_watching_rank=r,
            )
        )
    next_budget = ctrl.compute_next_budget(
        unique_normalized=16_000, cycle_interval_sec=3600, concurrency=10
    )
    # max=200, buffer = round(pstdev(ranks)) > 0 -> budget > 200
    assert next_budget > 200


def test_utility_window_truncates_old_observations():
    ctrl = BudgetController()
    # Old cycles had deep ranks; recent cycles much shallower.
    for _ in range(UTILITY_WINDOW * 2):
        ctrl.observe(
            CycleObservation(
                budget=400,
                scored=400,
                measured_duration_sec=100.0,
                cycle_interval_sec=3600.0,
                concurrency=10,
                max_watching_rank=400,
            )
        )
    for _ in range(UTILITY_WINDOW):
        ctrl.observe(
            CycleObservation(
                budget=400,
                scored=400,
                measured_duration_sec=100.0,
                cycle_interval_sec=3600.0,
                concurrency=10,
                max_watching_rank=80,
            )
        )
    next_budget = ctrl.compute_next_budget(
        unique_normalized=16_000, cycle_interval_sec=3600, concurrency=10
    )
    # Only the most recent UTILITY_WINDOW cycles should be considered.
    assert next_budget == BUDGET_FLOOR


# ---------------------------------------------------------------------------
# 5. Floor + ceiling clamping
# ---------------------------------------------------------------------------


def test_budget_never_drops_below_floor():
    ctrl = BudgetController()
    # Force AIMD shrinkage many times.
    for _ in range(50):
        ctrl.observe(
            CycleObservation(
                budget=BUDGET_FLOOR,
                scored=BUDGET_FLOOR,
                measured_duration_sec=10_000.0,
                cycle_interval_sec=3600.0,
                concurrency=10,
                max_watching_rank=None,
            )
        )
        budget = ctrl.compute_next_budget(
            unique_normalized=16_000, cycle_interval_sec=3600, concurrency=10
        )
        assert budget >= BUDGET_FLOOR


def test_budget_never_exceeds_universe_size():
    ctrl = BudgetController()
    ctrl.observe(
        CycleObservation(
            budget=500,
            scored=500,
            measured_duration_sec=10.0,
            cycle_interval_sec=3600.0,
            concurrency=10,
            max_watching_rank=None,
        )
    )
    next_budget = ctrl.compute_next_budget(
        unique_normalized=200, cycle_interval_sec=3600, concurrency=10
    )
    assert next_budget <= 200


# ---------------------------------------------------------------------------
# 6. Persistence round-trip
# ---------------------------------------------------------------------------


def test_save_then_load_preserves_state(tmp_path):
    ctrl = BudgetController()
    ctrl.observe(
        CycleObservation(
            budget=200,
            scored=200,
            measured_duration_sec=1500.0,
            cycle_interval_sec=3600.0,
            concurrency=10,
            max_watching_rank=80,
        )
    )
    ctrl.compute_next_budget(
        unique_normalized=16_000, cycle_interval_sec=3600, concurrency=10
    )
    p = tmp_path / "budget.json"
    save_budget_state(ctrl.state, path=p)
    loaded = load_budget_state(p)
    assert loaded.cycle_count == 1
    assert loaded.last_observation.get("budget") == 200
    assert loaded.history


def test_load_missing_file_returns_empty(tmp_path):
    state = load_budget_state(tmp_path / "nope.json")
    assert state.cycle_count == 0
    assert state.target_budget == 0


def test_load_corrupt_file_returns_empty(tmp_path):
    p = tmp_path / "budget.json"
    p.write_text("not json", encoding="utf-8")
    state = load_budget_state(p)
    assert state.cycle_count == 0


def test_save_atomic_no_tmp_files(tmp_path):
    state = BudgetState()
    save_budget_state(state, path=tmp_path / "subdir" / "budget.json")
    tmp_files = [pp for pp in (tmp_path / "subdir").iterdir() if pp.suffix == ".tmp"]
    assert tmp_files == []


# ---------------------------------------------------------------------------
# 7. Sanity on policy constants
# ---------------------------------------------------------------------------


def test_aimd_factors_match_policy():
    assert AIMD_INCREASE_FACTOR > 1.0
    assert AIMD_DECREASE_FACTOR < 1.0
    assert UNDER_UTIL_THRESHOLD < OVERRUN_THRESHOLD
    assert BUDGET_FLOOR >= 1
    assert BOOTSTRAP_SEC_PER_CALL >= 0.5
