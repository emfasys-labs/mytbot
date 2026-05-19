"""D118 — Self-tuning scoring-budget controller.

There is no hardcoded N here. The budget is the minimum of two
constraints measured from real cycle telemetry:

1. **Throughput**: an AIMD-style controller (TCP-like) measures the
   wall-clock fraction of the cycle interval that the last scoring
   pass consumed. Spent <80% of the interval -> grow budget by 5%.
   Spent >100% (overran) -> shrink budget by 15%. Otherwise stable.

2. **Utility saturation**: tracks the deepest priority-rank that
   actually entered the watching tier over the last N cycles. The
   budget never needs to go below that depth plus the rolling
   standard deviation. As soon as a more volatile regime pushes deeper
   ranks into watching, the buffer grows naturally.

The final budget is ``min(throughput, utility)`` clamped to a safety
floor (``BUDGET_FLOOR``) and a ceiling at the unique universe size.
These clamps are policy, not tuning knobs.
"""

from __future__ import annotations

import json
import math
import os
import statistics
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping


DEFAULT_BUDGET_PATH = Path("data/runtime/universe_budget_state.json")

# --- Policy constants (NOT operator-tunable; documented in DECISIONS.md D118) -
# AIMD step factors: increase on under-utilisation, decrease on overrun.
AIMD_INCREASE_FACTOR = 1.05
AIMD_DECREASE_FACTOR = 0.85
# Under-utilisation threshold (consume less than 80% of cycle interval).
UNDER_UTIL_THRESHOLD = 0.80
# Overrun threshold (consume more than 100% of cycle interval).
OVERRUN_THRESHOLD = 1.00
# Policy safety bounds (NOT operator-tunable; see DECISIONS.md D118).
BUDGET_FLOOR = 200
BUDGET_CEILING = 800
# Window over which utility saturation is measured (last N cycles).
UTILITY_WINDOW = 10
# Bootstrap assumption: one yfinance call takes about 2 seconds when
# we have no live telemetry yet. Replaced by measured values from
# cycle 2 onward.
BOOTSTRAP_SEC_PER_CALL = 2.0


@dataclass
class CycleObservation:
    """Telemetry the controller needs after every scoring cycle."""

    budget: int
    scored: int
    measured_duration_sec: float
    cycle_interval_sec: float
    concurrency: int
    max_watching_rank: int | None
    timestamp: str | None = None


@dataclass
class BudgetState:
    """Persisted controller state."""

    target_budget: int = 0
    last_observation: dict[str, object] = field(default_factory=dict)
    cycle_count: int = 0
    history: list[dict[str, object]] = field(default_factory=list)
    binding_constraint: str = "bootstrap"
    last_update_at: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "version": 1,
            "target_budget": int(self.target_budget),
            "last_observation": dict(self.last_observation),
            "cycle_count": int(self.cycle_count),
            "history": list(self.history),
            "binding_constraint": str(self.binding_constraint),
            "last_update_at": self.last_update_at,
        }


def bootstrap_budget(
    *,
    unique_normalized: int,
    concurrency: int,
    cycle_interval_sec: float,
) -> int:
    """One-time structural estimate when no observations exist yet.

    ``concurrency * cycle_interval / BOOTSTRAP_SEC_PER_CALL`` is the
    naive "how many calls fit if each one takes 2 seconds" upper bound.
    Capped at the unique universe size and floored at ``BUDGET_FLOOR``.
    """
    available = max(1, int(concurrency)) * max(1.0, float(cycle_interval_sec))
    raw = int(math.floor(available / max(0.1, BOOTSTRAP_SEC_PER_CALL)))
    capped = min(raw, max(0, int(unique_normalized)), BUDGET_CEILING)
    return max(BUDGET_FLOOR, capped) if unique_normalized > 0 else 0


def _policy_ceiling(unique_normalized: int) -> int:
    """Hard upper bound for the self-tuning budget."""
    return min(max(0, int(unique_normalized)), BUDGET_CEILING)


def _utility_budget(history: list[dict[str, object]]) -> tuple[int, int]:
    """Return (max_rank, buffer) over the last UTILITY_WINDOW cycles.

    Returns ``(0, 0)`` until we have a usable history. The buffer is
    the standard deviation of the rank observations so that volatile
    regimes (variable depth) automatically get a wider cushion.
    """
    recent = [
        int(entry.get("max_watching_rank") or 0)
        for entry in history[-UTILITY_WINDOW:]
        if isinstance(entry, Mapping) and entry.get("max_watching_rank") is not None
    ]
    if not recent:
        return (0, 0)
    max_rank = max(recent)
    if len(recent) >= 2:
        buffer = int(math.ceil(statistics.pstdev(recent)))
    else:
        buffer = 0
    return (max_rank, buffer)


class BudgetController:
    """Self-tuning scoring-budget controller.

    Holds the persisted state and exposes two operations:

    * :meth:`compute_next_budget` — given the universe size + cycle
      interval, compute the budget the next cycle should attempt.
    * :meth:`observe` — record the actual cycle telemetry so the next
      :meth:`compute_next_budget` call can adapt.
    """

    def __init__(self, state: BudgetState | None = None) -> None:
        self._state = state or BudgetState()

    @property
    def state(self) -> BudgetState:
        return self._state

    @property
    def target_budget(self) -> int:
        return int(self._state.target_budget)

    def compute_next_budget(
        self,
        *,
        unique_normalized: int,
        cycle_interval_sec: float,
        concurrency: int,
        now: datetime | None = None,
    ) -> int:
        """Return the budget for the next scoring cycle.

        On the very first cycle (no observations yet), this returns the
        bootstrap structural estimate. From cycle 2 onward, the budget
        is ``min(throughput_target, utility_target)`` clamped to the
        floor and the universe size.
        """
        ceiling = _policy_ceiling(unique_normalized)
        if ceiling <= 0:
            return 0
        # Bootstrap path: no measured cycle yet.
        if self._state.cycle_count <= 0 or self._state.target_budget <= 0:
            boot = bootstrap_budget(
                unique_normalized=ceiling,
                concurrency=concurrency,
                cycle_interval_sec=cycle_interval_sec,
            )
            self._state.binding_constraint = "bootstrap"
            self._state.last_update_at = (
                now or datetime.now(timezone.utc)
            ).astimezone(timezone.utc).isoformat()
            return max(BUDGET_FLOOR, min(boot, ceiling))
        # Throughput constraint from the most recent observation.
        last_obs = self._state.last_observation
        last_budget = int(last_obs.get("budget") or self._state.target_budget or BUDGET_FLOOR)
        utilization = float(last_obs.get("utilization") or 0.0)
        if utilization >= OVERRUN_THRESHOLD:
            throughput_next = int(math.floor(last_budget * AIMD_DECREASE_FACTOR))
            self._state.binding_constraint = "throughput"
        elif utilization < UNDER_UTIL_THRESHOLD:
            throughput_next = int(math.ceil(last_budget * AIMD_INCREASE_FACTOR))
            self._state.binding_constraint = "throughput"
        else:
            throughput_next = last_budget
            self._state.binding_constraint = "stable"
        # Utility constraint from rolling watching-tier depth.
        max_rank, buffer = _utility_budget(self._state.history)
        if max_rank > 0:
            # Rank is the index within the picked list, not a budget target;
            # never let a prior oversized cycle drive utility above policy.
            utility_next = min(max_rank + buffer, ceiling)
            if utility_next < throughput_next:
                self._state.binding_constraint = "utility"
        else:
            utility_next = throughput_next
        # Final clamp to the safety policy bounds.
        final = min(throughput_next, utility_next, ceiling)
        final = max(BUDGET_FLOOR, final)
        self._state.last_update_at = (
            now or datetime.now(timezone.utc)
        ).astimezone(timezone.utc).isoformat()
        return int(final)

    def observe(self, observation: CycleObservation, *, now: datetime | None = None) -> None:
        """Record a completed cycle's telemetry."""
        cycle_interval = max(1.0, float(observation.cycle_interval_sec))
        utilization = float(observation.measured_duration_sec) / cycle_interval
        ts = (
            observation.timestamp
            or (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
        )
        budget_n = int(observation.budget)
        max_rank = (
            int(observation.max_watching_rank)
            if observation.max_watching_rank is not None
            else None
        )
        if max_rank is not None and budget_n > 0:
            max_rank = min(max_rank, budget_n, BUDGET_CEILING)
        entry = {
            "ts": ts,
            "budget": budget_n,
            "scored": int(observation.scored),
            "measured_duration_sec": float(observation.measured_duration_sec),
            "cycle_interval_sec": float(cycle_interval),
            "utilization": float(utilization),
            "concurrency": int(observation.concurrency),
            "max_watching_rank": max_rank,
        }
        self._state.history.append(entry)
        if len(self._state.history) > UTILITY_WINDOW * 5:
            self._state.history = self._state.history[-UTILITY_WINDOW * 5:]
        self._state.last_observation = entry
        self._state.cycle_count += 1
        # Pre-compute the next target so dashboards see a fresh figure
        # immediately, even before the next ``compute_next_budget`` call.
        unique_norm = int(observation.budget) if observation.budget > 0 else BUDGET_FLOOR
        # NB: we cannot know ``unique_normalized`` here without re-
        # passing it; callers always invoke ``compute_next_budget`` on
        # the next tick which performs the real update. We just stamp
        # the current target so consumers do not see stale 0.
        self._state.target_budget = max(BUDGET_FLOOR, int(observation.budget))
        self._state.last_update_at = ts


def load_budget_state(path: Path | None = None) -> BudgetState:
    """Read the persisted controller state; returns empty state on any error."""
    p = path or DEFAULT_BUDGET_PATH
    if not p.is_file():
        return BudgetState()
    try:
        blob = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return BudgetState()
    if not isinstance(blob, Mapping):
        return BudgetState()
    history_raw = blob.get("history")
    history: list[dict[str, object]] = []
    if isinstance(history_raw, list):
        for entry in history_raw:
            if isinstance(entry, Mapping):
                history.append(dict(entry))
    last_obs = blob.get("last_observation")
    return BudgetState(
        target_budget=int(blob.get("target_budget") or 0),
        last_observation=dict(last_obs) if isinstance(last_obs, Mapping) else {},
        cycle_count=int(blob.get("cycle_count") or 0),
        history=history,
        binding_constraint=str(blob.get("binding_constraint") or "bootstrap"),
        last_update_at=(
            str(blob.get("last_update_at")) if blob.get("last_update_at") else None
        ),
    )


def save_budget_state(state: BudgetState, *, path: Path | None = None) -> Path:
    """Atomically persist the controller state."""
    p = path or DEFAULT_BUDGET_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(state.to_dict(), indent=2, sort_keys=True)
    tmp = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        suffix=".tmp",
        dir=str(p.parent),
        delete=False,
    )
    try:
        tmp.write(payload)
        tmp.flush()
        os.fsync(tmp.fileno())
    finally:
        tmp.close()
    os.replace(tmp.name, str(p))
    return p
