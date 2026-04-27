"""
models/forecasts/ensemble.py
==============================
Wave 6 — multi-horizon ensemble blending.

D015's ``Opportunity.expected_return`` and ``confidence`` need a single
numeric pair, but our forecast stack emits multiple targets (1h, 4h,
1d return; vol; drawdown). The ensemble combines them.

Convention:

- Regression members (forward returns, vol forecast) contribute to
  ``expected_return`` directly (signed, weighted average across
  horizons; vol forecast contributes to ``expected_volatility``).
- Classification members (breakout, mean-reversion success, drawdown)
  contribute to ``confidence`` (their probabilities are blended via
  weighted geometric mean centred on 0.5 — i.e. extreme members move
  the needle harder than mid-range ones).

The ensemble is deliberately simple. Wave 11 (deep sequence models)
can introduce a learned blender; Wave 8 portfolio optimisation can
consume `expected_return / expected_volatility` directly.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterable, Optional

from models.forecasts.targets import is_classification_target


@dataclass(frozen=True)
class EnsembleMember:
    target_kind: str
    horizon: int
    value: float
    weight: float = 1.0
    model_name: Optional[str] = None


@dataclass
class EnsembleResult:
    expected_return: Optional[float] = None
    expected_volatility: Optional[float] = None
    confidence: Optional[float] = None  # in [0, 1]
    horizons_used: tuple[int, ...] = ()
    contributions: dict[str, float] = field(default_factory=dict)


@dataclass
class ForecastEnsemble:
    """Combine multiple forecasts into a single (return, vol, confidence) triple."""

    @staticmethod
    def combine(members: Iterable[EnsembleMember]) -> EnsembleResult:
        rs: list[tuple[float, float, str]] = []  # (value, weight, label)
        vols: list[tuple[float, float]] = []
        probs: list[tuple[float, float, str]] = []  # (prob, weight, kind)
        horizons: list[int] = []
        contributions: dict[str, float] = {}

        for m in members:
            if not math.isfinite(m.value) or m.weight <= 0:
                continue
            kind = (m.target_kind or "").strip().lower()
            horizons.append(int(m.horizon))
            label = f"{kind}_h{m.horizon}"
            contributions[label] = float(m.value)

            if kind == "forward_return":
                # Down-weight longer horizons very mildly so the 1h forecast
                # isn't drowned by a confident 1d call.
                w = m.weight / max(1.0, math.log1p(max(0, m.horizon)))
                rs.append((float(m.value), w, label))
            elif kind == "realised_vol_forward":
                vols.append((float(m.value), m.weight))
            elif is_classification_target(kind):
                p = max(0.0, min(1.0, float(m.value)))
                # For drawdown_probability higher is *worse*; flip so the
                # ensemble's confidence stays in "this trade looks fine"
                # semantics.
                if kind == "drawdown_probability":
                    p = 1.0 - p
                probs.append((p, m.weight, kind))
            # Unknown kinds are ignored.

        # Aggregate regression returns.
        exp_ret: Optional[float] = None
        if rs:
            tot_w = sum(w for _, w, _ in rs)
            if tot_w > 0:
                exp_ret = sum(v * w for v, w, _ in rs) / tot_w

        # Aggregate forward vol — geometric mean weighted (vol is positive).
        exp_vol: Optional[float] = None
        if vols:
            tot_w = sum(w for _, w in vols)
            if tot_w > 0:
                exp_vol = math.exp(
                    sum(math.log(max(1e-12, v)) * w for v, w in vols) / tot_w
                )

        # Aggregate confidence — weighted geometric mean of (p, 1-p) shaped.
        confidence: Optional[float] = None
        if probs:
            tot_w = sum(w for _, w, _ in probs)
            if tot_w > 0:
                # Soft-OR-style aggregation: log-odds weighted average then sigmoid.
                logits = []
                for p, w, _ in probs:
                    p_clip = max(1e-6, min(1 - 1e-6, p))
                    logits.append((math.log(p_clip / (1 - p_clip)), w))
                wsum = sum(w for _, w in logits)
                z = sum(l * w for l, w in logits) / wsum if wsum > 0 else 0.0
                confidence = 1.0 / (1.0 + math.exp(-z))

        return EnsembleResult(
            expected_return=exp_ret,
            expected_volatility=exp_vol,
            confidence=confidence,
            horizons_used=tuple(sorted(set(horizons))),
            contributions=contributions,
        )
