"""D118 — Priority pre-filter for the dynamic universe.

Pure, deterministic, in-memory scoring of EVERY unique normalized
symbol the brokers + registry expose, so the scoring budget (which is
itself self-tuned by :mod:`data.universe_budget_controller`) can be
spent on the top-N by ``priority_score`` instead of a random stratified
sample.

The score is a weighted sum of six 0..1 components. Weights are NOT in
this module — they live in :mod:`data.universe_weight_learner`, which
updates them online from the post-cycle observation "did this pick
enter the watching tier?". This module just consumes the current
weights and computes the score.

There is no randomness anywhere in this file. Ties on ``priority_score``
are broken stably by symbol name ascending.

Components
----------

``liquidity_prior``       0..1 — previous yfinance liquidity score.
                           Never-scored symbols default to a neutral
                           0.5 so the cycle-1 bootstrap is sane.

``anchor_pin``            0 or 1 — curated anchor membership.

``freshness_bonus``       0..1 — log-decay of seconds since
                           ``last_scored_at``. Never-scored = 1.0,
                           just-scored = 0.0.

``registry_availability`` 0..1 — D116 availability:
                           ``available`` 1.0,
                           ``requires_qualification`` 0.7,
                           ``unknown`` 0.3,
                           ``unavailable`` / ``blocked`` 0.0.

``asset_class_balance``   0..1 — pushes under-represented asset
                           classes up so a 30:1 equities-heavy
                           watching tier does not starve bonds/FX.

``region_balance``        0..1 — same idea but by region.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable, Mapping

from data.universe_score_ages import ScoreAges


# Default freshness half-life. This is the safety floor for bootstrap
# only; the weight learner cannot reach into freshness directly so the
# decay shape is a fixed policy. Documented in DECISIONS.md D118.
DEFAULT_FRESHNESS_HALF_LIFE_SEC = 24.0 * 3600.0  # 24h

# The neutral liquidity prior for symbols we have never scored. Halfway
# between 0 and 1 so it is dominated by other components when registry
# and balance data exist.
NEVER_SCORED_LIQUIDITY_PRIOR = 0.5

# Availability mapping used to compute the registry component. These
# values mirror the documented D116 status enum (see
# ``instruments.registry.AVAILABILITY_STATES``).
AVAILABILITY_SCORE: Mapping[str, float] = {
    "available": 1.0,
    "requires_qualification": 0.7,
    "unknown": 0.3,
    "unavailable": 0.0,
    "blocked": 0.0,
}

# Component names in the canonical order used by the learner. Any new
# component MUST be appended here and to PriorityBreakdown so the
# learner's online weight vector stays aligned by index.
COMPONENT_NAMES: tuple[str, ...] = (
    "liquidity_prior",
    "anchor_pin",
    "freshness_bonus",
    "registry_availability",
    "asset_class_balance",
    "region_balance",
)


@dataclass(frozen=True)
class PriorityBreakdown:
    """Per-symbol breakdown of the priority pre-filter."""

    symbol: str
    priority_score: float
    components: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "priority_score": float(self.priority_score),
            "components": {k: float(v) for k, v in self.components.items()},
        }


@dataclass(frozen=True)
class AvailabilityHint:
    """Minimal registry projection used by the pre-filter.

    The full ``instruments.registry.AvailabilityRow`` carries broker
    identity and timestamps that the pre-filter does not need; we keep
    only the canonical-symbol-level fields and one ``best status``.
    """

    canonical_symbol: str
    asset_class: str | None = None
    region: str | None = None
    best_status: str = "unknown"


def normalise_symbol(symbol: str) -> str:
    return str(symbol or "").strip().upper()


def uniform_weights() -> dict[str, float]:
    """Bootstrap weights: uniform 1/N across components."""
    n = len(COMPONENT_NAMES)
    return {name: 1.0 / float(n) for name in COMPONENT_NAMES}


def _balance_component(
    counts_in_watching: Mapping[str, int],
    *,
    bucket: str | None,
    floor: float = 0.0,
) -> float:
    """0..1 score that lifts under-represented buckets.

    Uses inverse-share of the bucket in the current watching tier. An
    unknown bucket gets the floor so we do not punish symbols whose
    registry data is missing.
    """
    if not bucket:
        return floor
    total = sum(int(c) for c in counts_in_watching.values() if c) or 0
    if total <= 0:
        # Watching is empty: nothing is over-represented yet.
        return 0.5
    share = float(counts_in_watching.get(bucket, 0)) / float(total)
    return max(0.0, min(1.0, 1.0 - share))


def _freshness_component(
    age_seconds: float | None,
    *,
    half_life_sec: float = DEFAULT_FRESHNESS_HALF_LIFE_SEC,
) -> float:
    """0..1 log-decay; never-scored -> 1.0; just-scored -> 0.0."""
    if age_seconds is None:
        return 1.0
    if age_seconds <= 0.0:
        return 0.0
    half_life = max(60.0, float(half_life_sec))
    decay = 1.0 - math.exp(-math.log(2.0) * (age_seconds / half_life))
    return max(0.0, min(1.0, decay))


def _registry_component(status: str | None) -> float:
    key = (status or "unknown").strip().lower()
    return float(AVAILABILITY_SCORE.get(key, AVAILABILITY_SCORE["unknown"]))


def _liquidity_component(
    last_score: float | None,
    *,
    score_ceiling: float,
) -> float:
    if last_score is None:
        return NEVER_SCORED_LIQUIDITY_PRIOR
    if score_ceiling <= 0.0:
        return NEVER_SCORED_LIQUIDITY_PRIOR
    return max(0.0, min(1.0, float(last_score) / float(score_ceiling)))


def _normalise_weights(weights: Mapping[str, float]) -> dict[str, float]:
    """Defensive normaliser: clamp to 0..1, drop unknowns, re-normalise to sum 1."""
    raw = {
        name: max(0.0, float(weights.get(name, 0.0) or 0.0))
        for name in COMPONENT_NAMES
    }
    total = sum(raw.values())
    if total <= 0.0:
        return uniform_weights()
    return {name: value / total for name, value in raw.items()}


def _bucket_counts(
    watching: Iterable[str],
    *,
    hints: Mapping[str, AvailabilityHint],
    key: str,
) -> dict[str, int]:
    counts: dict[str, int] = {}
    for sym in watching:
        norm = normalise_symbol(sym)
        hint = hints.get(norm)
        if hint is None:
            continue
        value = getattr(hint, key, None)
        if not value:
            continue
        bucket = str(value).strip().lower()
        if not bucket:
            continue
        counts[bucket] = counts.get(bucket, 0) + 1
    return counts


def compute_priority_scores(
    symbols: Iterable[str],
    *,
    score_ages: ScoreAges,
    weights: Mapping[str, float],
    anchors: Iterable[str] = (),
    watching_now: Iterable[str] = (),
    availability_hints: Mapping[str, AvailabilityHint] | None = None,
    freshness_half_life_sec: float = DEFAULT_FRESHNESS_HALF_LIFE_SEC,
    score_ceiling: float | None = None,
    now: datetime | None = None,
) -> dict[str, PriorityBreakdown]:
    """Compute the priority score and component breakdown per symbol.

    Parameters
    ----------
    symbols
        The unique normalized universe. Duplicates are ignored.
    score_ages
        Persisted score-age telemetry (read-only here).
    weights
        Current learned weights. Defensive normaliser will clamp + sum
        to 1.0 even if the caller drifted.
    anchors
        Curated anchor symbols (1.0 in the ``anchor_pin`` component).
    watching_now
        The current watching tier — used to compute the
        ``asset_class_balance`` / ``region_balance`` inverse-share
        components.
    availability_hints
        Optional registry-derived projection per canonical symbol.
    freshness_half_life_sec
        Log-decay half-life for ``freshness_bonus``.
    score_ceiling
        Optional max liquidity score used to normalise
        ``liquidity_prior`` to 0..1. If ``None``, derived from the
        highest persisted ``last_score`` across all known symbols. A
        floor of 1.0 is applied so we never divide by zero.

    Returns a dict ``{symbol -> PriorityBreakdown}``.
    """
    norm_weights = _normalise_weights(weights)
    anchor_set = {normalise_symbol(a) for a in anchors if a}
    hints = {normalise_symbol(k): v for k, v in (availability_hints or {}).items()}
    watching_set = [normalise_symbol(s) for s in watching_now if s]
    asset_class_counts = _bucket_counts(watching_set, hints=hints, key="asset_class")
    region_counts = _bucket_counts(watching_set, hints=hints, key="region")

    # Derive the ceiling once per call if not supplied; this avoids
    # making the function dependent on global state and keeps it pure.
    if score_ceiling is None:
        derived_max = 0.0
        for _, row in score_ages.items():
            if row.last_score is not None and row.last_score > derived_max:
                derived_max = float(row.last_score)
        ceiling = max(1.0, derived_max)
    else:
        ceiling = max(1.0, float(score_ceiling))

    seen: set[str] = set()
    out: dict[str, PriorityBreakdown] = {}
    for raw_sym in symbols:
        sym = normalise_symbol(raw_sym)
        if not sym or sym in seen:
            continue
        seen.add(sym)
        hint = hints.get(sym)
        age = score_ages.age_seconds_for(sym, now=now)
        components = {
            "liquidity_prior": _liquidity_component(
                score_ages.last_score_for(sym), score_ceiling=ceiling
            ),
            "anchor_pin": 1.0 if sym in anchor_set else 0.0,
            "freshness_bonus": _freshness_component(
                age, half_life_sec=freshness_half_life_sec
            ),
            "registry_availability": _registry_component(
                hint.best_status if hint else None
            ),
            "asset_class_balance": _balance_component(
                asset_class_counts,
                bucket=(hint.asset_class if hint else None),
            ),
            "region_balance": _balance_component(
                region_counts,
                bucket=(hint.region if hint else None),
            ),
        }
        priority = sum(
            float(norm_weights.get(name, 0.0)) * float(components[name])
            for name in COMPONENT_NAMES
        )
        out[sym] = PriorityBreakdown(
            symbol=sym,
            priority_score=float(priority),
            components=components,
        )
    return out


def top_n_by_priority(
    breakdowns: Mapping[str, PriorityBreakdown],
    *,
    budget: int,
    pinned: Iterable[str] = (),
) -> list[str]:
    """Select the top-N symbols by ``priority_score`` deterministically.

    Ties on ``priority_score`` are broken stably by symbol name ASC so
    the same input always produces the same selection. ``pinned``
    symbols are guaranteed in the output as long as they exist in the
    breakdown map — they consume slots from the budget but are never
    silently dropped.
    """
    if budget <= 0:
        return []
    pinned_norm = [normalise_symbol(p) for p in pinned if p]
    pinned_set: set[str] = set()
    selected: list[str] = []
    for sym in pinned_norm:
        if sym in breakdowns and sym not in pinned_set:
            pinned_set.add(sym)
            selected.append(sym)
            if len(selected) >= budget:
                return selected
    remaining = [
        (sym, bd) for sym, bd in breakdowns.items() if sym not in pinned_set
    ]
    remaining.sort(key=lambda item: (-item[1].priority_score, item[0]))
    for sym, _ in remaining:
        selected.append(sym)
        if len(selected) >= budget:
            break
    return selected
