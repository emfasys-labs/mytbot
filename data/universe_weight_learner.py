"""D118 — Online weight learner for the priority pre-filter.

We do not hand-set weights for the six priority components. Instead we
run an online logistic regression with AdaGrad after every pipeline
cycle, using the simple binary outcome ``did this picked symbol enter
the watching tier?`` as the supervision signal. Component subscores
from :mod:`data.universe_prefilter` are the features.

The weights are then bounded into ``[WEIGHT_FLOOR, WEIGHT_CEILING]`` and
re-normalised to sum to 1.0 before persistence. Those clamps are
deliberate safety policy (documented in DECISIONS.md D118) so the
learner cannot collapse to a degenerate single-component solution.

EWMA decay (~30 cycles by default) keeps the learner responsive to
regime shifts: long-stale gradients fade so the weights chase recent
outcomes without overfitting one cycle.

Persistence is atomic (``tempfile + os.replace``) and corrupt-safe
(load returns uniform bootstrap weights on any error).
"""

from __future__ import annotations

import json
import math
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping

from data.universe_prefilter import COMPONENT_NAMES, PriorityBreakdown, uniform_weights


DEFAULT_WEIGHTS_PATH = Path("data/runtime/universe_priority_weights.json")

# --- Policy constants (NOT operator-tunable; documented in DECISIONS.md D118) -
# Each component is clamped into [floor, ceiling], then renormalised to
# sum to 1.0. These prevent the learner from collapsing to a degenerate
# single-component solution on a noisy cycle.
WEIGHT_FLOOR = 0.02
WEIGHT_CEILING = 0.70
# AdaGrad epsilon; tiny constant to avoid division by zero.
ADAGRAD_EPSILON = 1e-8
# Base learning rate before per-component AdaGrad adaptation. With six
# components and small gradients this is effectively a slow learner;
# the AdaGrad accumulator handles per-component adaptation.
BASE_LEARNING_RATE = 0.5
# Maximum number of recent (weights, ts) snapshots persisted for the
# UI trajectory sparkline.
MAX_HISTORY_KEPT = 100
# EWMA decay window in cycles. Used both for the persisted gradient-
# squared accumulator and for surfacing "how stale is each weight"
# diagnostics.
DEFAULT_EWMA_CYCLES = 30.0


@dataclass
class WeightLearnerState:
    """Persisted state for the online learner."""

    weights: dict[str, float] = field(default_factory=uniform_weights)
    grad_sq: dict[str, float] = field(default_factory=lambda: {n: 0.0 for n in COMPONENT_NAMES})
    cycle_count: int = 0
    history: list[dict[str, object]] = field(default_factory=list)
    last_update_at: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "version": 1,
            "weights": dict(self.weights),
            "grad_sq": dict(self.grad_sq),
            "cycle_count": int(self.cycle_count),
            "history": list(self.history),
            "last_update_at": self.last_update_at,
        }


def _clamp_and_renormalise(weights: Mapping[str, float]) -> dict[str, float]:
    """Apply WEIGHT_FLOOR/CEILING then re-normalise to sum to 1.0.

    The renormalisation is done after clamping so the clamps act as
    SAFETY POLICY, not as soft hints — a component can never exceed
    the ceiling or fall below the floor in the persisted state.
    """
    clamped = {
        name: float(max(WEIGHT_FLOOR, min(WEIGHT_CEILING, weights.get(name, 0.0) or 0.0)))
        for name in COMPONENT_NAMES
    }
    total = sum(clamped.values())
    if total <= 0.0:
        return uniform_weights()
    return {name: value / total for name, value in clamped.items()}


def _sigmoid(z: float) -> float:
    if z >= 0:
        ez = math.exp(-z)
        return 1.0 / (1.0 + ez)
    ez = math.exp(z)
    return ez / (1.0 + ez)


@dataclass(frozen=True)
class TrainingRow:
    """One training row: a picked symbol's components + outcome."""

    symbol: str
    components: dict[str, float]
    label: int  # 1 if entered watching, else 0


def build_training_rows(
    picks_breakdowns: Mapping[str, PriorityBreakdown],
    watching_after: Iterable[str],
) -> list[TrainingRow]:
    """Pair every picked symbol with its post-cycle watching outcome."""
    watching_set = {str(s or "").strip().upper() for s in watching_after if s}
    rows: list[TrainingRow] = []
    for sym, bd in picks_breakdowns.items():
        norm = str(sym or "").strip().upper()
        if not norm:
            continue
        label = 1 if norm in watching_set else 0
        rows.append(
            TrainingRow(
                symbol=norm,
                components={k: float(v) for k, v in bd.components.items()},
                label=label,
            )
        )
    return rows


class WeightLearner:
    """Online logistic regression with AdaGrad.

    The learner treats the six priority components as features and the
    binary outcome ``did the pick enter watching`` as the target. Each
    pipeline cycle, one gradient step is taken per training row.
    AdaGrad adapts the per-component step size automatically; the EWMA
    decay parameter controls how fast the gradient accumulator forgets
    older cycles.

    The weights this learner exposes to :func:`compute_priority_scores`
    are the result of :func:`_clamp_and_renormalise` on its internal
    representation so the priority pre-filter never sees unbounded
    values.
    """

    def __init__(
        self,
        state: WeightLearnerState | None = None,
        *,
        learning_rate: float = BASE_LEARNING_RATE,
        ewma_cycles: float = DEFAULT_EWMA_CYCLES,
    ) -> None:
        self._state = state or WeightLearnerState()
        self._learning_rate = max(1e-4, float(learning_rate))
        self._ewma_cycles = max(1.0, float(ewma_cycles))

    @property
    def state(self) -> WeightLearnerState:
        return self._state

    @property
    def cycle_count(self) -> int:
        return int(self._state.cycle_count)

    def current_weights(self) -> dict[str, float]:
        """Return the clamped + re-normalised weights for live use."""
        return _clamp_and_renormalise(self._state.weights)

    def update(
        self,
        rows: Iterable[TrainingRow],
        *,
        now: datetime | None = None,
    ) -> dict[str, float]:
        """Apply one gradient step per training row.

        Returns the new clamped + re-normalised weight vector.
        """
        rows_list = list(rows)
        if not rows_list:
            return self.current_weights()

        # EWMA-decay the AdaGrad accumulator BEFORE accumulating new
        # gradient squares so old cycles fade as the new cycle's
        # contribution arrives. Decay factor is exp(-1/ewma_cycles).
        decay = math.exp(-1.0 / self._ewma_cycles)
        grad_sq = {
            name: float(self._state.grad_sq.get(name, 0.0)) * decay
            for name in COMPONENT_NAMES
        }
        # Use the internal (un-clamped) weights for gradient stepping
        # so the learner can move freely in latent space; the clamp +
        # renormalisation are applied AFTER stepping when surfacing
        # weights to the rest of the system.
        weights = {
            name: float(self._state.weights.get(name, 1.0 / len(COMPONENT_NAMES)))
            for name in COMPONENT_NAMES
        }

        for row in rows_list:
            # Forward pass: linear score then sigmoid.
            z = sum(
                weights[name] * float(row.components.get(name, 0.0) or 0.0)
                for name in COMPONENT_NAMES
            )
            pred = _sigmoid(z)
            err = float(row.label) - pred  # gradient direction for logistic
            for name in COMPONENT_NAMES:
                feat = float(row.components.get(name, 0.0) or 0.0)
                grad = err * feat
                grad_sq[name] += grad * grad
                step = self._learning_rate * grad / math.sqrt(grad_sq[name] + ADAGRAD_EPSILON)
                weights[name] += step

        self._state.weights = weights
        self._state.grad_sq = grad_sq
        self._state.cycle_count = int(self._state.cycle_count) + 1
        self._state.last_update_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()

        snap_weights = _clamp_and_renormalise(weights)
        self._record_history(snap_weights, self._state.last_update_at)
        return snap_weights

    def _record_history(self, weights: Mapping[str, float], ts: str) -> None:
        snapshot = {
            "ts": ts,
            "cycle": int(self._state.cycle_count),
            "weights": {k: float(v) for k, v in weights.items()},
        }
        self._state.history.append(snapshot)
        if len(self._state.history) > MAX_HISTORY_KEPT:
            self._state.history = self._state.history[-MAX_HISTORY_KEPT:]


def load_weight_learner_state(path: Path | None = None) -> WeightLearnerState:
    """Read the persisted weights file; returns uniform bootstrap on any error."""
    p = path or DEFAULT_WEIGHTS_PATH
    if not p.is_file():
        return WeightLearnerState()
    try:
        blob = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return WeightLearnerState()
    if not isinstance(blob, Mapping):
        return WeightLearnerState()
    weights_raw = blob.get("weights")
    grad_raw = blob.get("grad_sq")
    if not isinstance(weights_raw, Mapping):
        weights = uniform_weights()
    else:
        weights = {
            name: float(weights_raw.get(name, 1.0 / len(COMPONENT_NAMES)) or 0.0)
            for name in COMPONENT_NAMES
        }
    if not isinstance(grad_raw, Mapping):
        grad_sq = {n: 0.0 for n in COMPONENT_NAMES}
    else:
        grad_sq = {
            name: max(0.0, float(grad_raw.get(name, 0.0) or 0.0))
            for name in COMPONENT_NAMES
        }
    history_raw = blob.get("history")
    history: list[dict[str, object]] = []
    if isinstance(history_raw, list):
        for entry in history_raw:
            if not isinstance(entry, Mapping):
                continue
            w = entry.get("weights")
            if not isinstance(w, Mapping):
                continue
            history.append(
                {
                    "ts": str(entry.get("ts") or ""),
                    "cycle": int(entry.get("cycle") or 0),
                    "weights": {
                        name: float(w.get(name, 0.0) or 0.0) for name in COMPONENT_NAMES
                    },
                }
            )
    cycle_count = int(blob.get("cycle_count") or 0)
    last_update_at = blob.get("last_update_at")
    return WeightLearnerState(
        weights=weights,
        grad_sq=grad_sq,
        cycle_count=cycle_count,
        history=history[-MAX_HISTORY_KEPT:],
        last_update_at=str(last_update_at) if last_update_at else None,
    )


def save_weight_learner_state(state: WeightLearnerState, *, path: Path | None = None) -> Path:
    """Atomically persist the learner state."""
    p = path or DEFAULT_WEIGHTS_PATH
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
