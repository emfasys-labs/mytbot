"""
system/adaptive_edge.py
========================
Compute the displacement-gate edge threshold dynamically from current
execution costs, rather than reading a static per-mode number from YAML.

The previous behaviour was ``edge_advantage: {hunter: 0.02, trader: 0.05,
defender: 0.12}`` — three guessed constants. Each one bundles a cost
estimate and a cushion together with no traceability:
"is 5% the right bar in May 2026 with 0.5 bps Alpaca fees?"
"why doesn't it follow the venue I'm trading on?"

This module replaces that with two ideas that are first-principles:

1. **Cost grounding.** The threshold has to clear the round-trip cost
   of getting in and out. We read the same fee + spread + slippage
   priors the Wave 9 cost gate uses, average across active venues, and
   use ``2 × that cost`` as the floor.

2. **Outcome-aware cushion.** A multiplier on top of the floor that
   moves with realised behaviour:
       * recent winners → cushion shrinks toward 1.0 (more aggressive)
       * recent losers → cushion expands (up to a cap) — back off
       * Hunter mode biases the cushion lower (we want to be in trades);
         defender biases it higher (we want to be selective).

The result is a single ``Decimal`` threshold the coordinator can use
exactly as it does today, so the wiring is one-line and reversible.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional


@dataclass(frozen=True)
class EdgeThresholdInputs:
    """Snapshot for the threshold calculator. Same robustness contract as
    ``ModeInputs`` / ``CadenceInputs`` — missing values fall through.
    """

    # Mode label from adaptive_mode (hunter / trader / defender).
    mode: str = "hunter"
    # Average expected round-trip cost across active venues, in bps.
    # ``fee_bps + spread_bps + slippage_bps`` (impact handled at gate-time
    # per-trade; here we want a coarse all-symbol baseline).
    cross_venue_cost_bps: Optional[float] = None
    # Rolling realised win-rate from recent round-trip closes (0.0–1.0).
    recent_win_rate: Optional[float] = None
    # Rolling realised P&L per close, signed, in fractional return.
    recent_avg_return: Optional[float] = None
    # Static YAML floor — never go below it. Defaults to the trader value
    # so a missing config doesn't accidentally weaken the gate.
    static_floor: float = 0.05


_MIN_THRESHOLD = float(os.getenv("ADAPTIVE_EDGE_MIN", "0.002"))  # 20 bps absolute floor
_MAX_THRESHOLD = float(os.getenv("ADAPTIVE_EDGE_MAX", "0.30"))   # 30% absolute ceiling
_DEFAULT_COST_BPS = float(os.getenv("ADAPTIVE_EDGE_DEFAULT_COST_BPS", "10"))

# ── Balanced adaptive turnover governor ──────────────────────────────────
# The cushion widens *continuously* as realised edge decays / the regime
# turns choppy — so churn throttles itself BEFORE the day goes red, not
# only after. All coefficients are env-tunable; the defaults are
# deliberately mild ("balanced": damp the downside, keep trend upside) and
# the result is still floored by ``static_floor`` and clamped, so this can
# never trade more aggressively than the operator's existing setting.
_CUSHION_MIN = float(os.getenv("ADAPTIVE_EDGE_CUSHION_MIN", "0.85"))
_CUSHION_MAX = float(os.getenv("ADAPTIVE_EDGE_CUSHION_MAX", "2.20"))
# Win-rate pivot: at/above this we don't penalise; below it the cushion
# grows ~linearly. 0.52 ≈ a touch better than a coin flip after costs.
_WR_PIVOT = float(os.getenv("ADAPTIVE_EDGE_WR_PIVOT", "0.52"))
_WR_GAIN = float(os.getenv("ADAPTIVE_EDGE_WR_GAIN", "3.0"))
# Return scale (fractional). avg_ret is squashed by this: a per-close
# realised return this size moves the cushion by ~one unit. 0.004 ≈ 40 bps.
_RET_SCALE = float(os.getenv("ADAPTIVE_EDGE_RET_SCALE", "0.004"))
_RET_GAIN = float(os.getenv("ADAPTIVE_EDGE_RET_GAIN", "0.9"))


def _mode_cushion_bias(mode: str) -> float:
    """Hunter is fast and accepts thinner edges; defender is choosy."""
    m = (mode or "hunter").strip().lower()
    if m == "defender":
        return 1.8  # need 80% more edge than cost alone before opening
    if m == "trader":
        return 1.4
    return 1.0  # hunter — just clear the cost


def _outcome_cushion(win_rate: Optional[float], avg_ret: Optional[float]) -> float:
    """Translate recent realised outcomes into a cushion multiplier.

    **Balanced adaptive governor.** A *continuous, monotonic* function of
    recent win-rate and realised per-close return — not the old coarse
    3-step. The point is that a decaying-but-still-positive edge
    (+$10k → +$6k → +$2k …) starts lifting the bar *while it is still
    green*, so high-turnover rotation throttles itself before the day
    turns red. Properties this guarantees (and the tests pin):

      * never *decreases* when win-rate falls or avg-return falls
        (monotone — more decay ⇒ never thinner edge required);
      * strong winners still relax toward ``_CUSHION_MIN`` (keep the
        trend-day upside the user explicitly wanted to preserve);
      * bounded to ``[_CUSHION_MIN, _CUSHION_MAX]``;
      * returns 1.0 on missing inputs (fresh boot has no statistics).
    """
    if win_rate is None or avg_ret is None:
        return 1.0

    # Win-rate term: 0 at/above pivot, growing as it drops below.
    wr_excess = _WR_PIVOT - float(win_rate)          # >0 when below pivot
    wr_term = _WR_GAIN * max(0.0, wr_excess)

    # Return term: a smooth, bounded S-curve in avg_ret / scale. Strong
    # positive → negative term (relax toward CUSHION_MIN); as the realised
    # edge decays toward 0 the term rises through 0 and goes positive
    # *before* avg_ret turns negative, then keeps widening when it does.
    x = float(avg_ret) / _RET_SCALE if _RET_SCALE > 0 else 0.0
    x = max(-50.0, min(50.0, x))                     # overflow guard
    squashed = math.tanh(x)                          # (-1, 1)
    ret_term = -_RET_GAIN * squashed                 # winners shrink, decay widens

    cushion = 1.0 + wr_term + ret_term
    return max(_CUSHION_MIN, min(_CUSHION_MAX, cushion))


def compute_edge_threshold(inputs: EdgeThresholdInputs) -> Decimal:
    """Return the displacement edge threshold as a fractional return.

    Formula:
        cost = cross_venue_cost_bps or _DEFAULT_COST_BPS
        cushion = mode_bias × outcome_cushion
        threshold_frac = 2 × cost_bps × cushion / 10000
        return max(static_floor, threshold_frac), clamped to [MIN, MAX]

    Why ``2 × cost``: round-trip = entry + exit, each pays the cost
    once. The cushion then layers risk preference on top.

    Why ``max(static_floor, ...)``: until Phase 5 strips the YAML, the
    operator's existing static number is a safety net. We never go more
    aggressive than that until the operator opts in by lowering it.
    """
    cost_bps = inputs.cross_venue_cost_bps
    if cost_bps is None or cost_bps <= 0:
        cost_bps = _DEFAULT_COST_BPS
    cushion = _mode_cushion_bias(inputs.mode) * _outcome_cushion(
        inputs.recent_win_rate, inputs.recent_avg_return,
    )
    threshold_frac = (2.0 * cost_bps * cushion) / 10_000.0
    threshold_frac = max(inputs.static_floor, threshold_frac)
    threshold_frac = max(_MIN_THRESHOLD, min(_MAX_THRESHOLD, threshold_frac))
    return Decimal(str(round(threshold_frac, 6)))


def estimate_cross_venue_cost_bps(
    venue_priors: "object | None",
    slippage_model: "object | None",
    active_brokers: list[str],
    active_asset_classes: list[str],
) -> Optional[float]:
    """Coarse average ``fee + spread + slippage`` across active venues.

    Returns ``None`` when no inputs are available so the threshold can
    fall back to the static floor.
    """
    if not active_brokers or not active_asset_classes:
        return None
    samples: list[float] = []
    for broker in active_brokers:
        for ac in active_asset_classes:
            try:
                fee_bps = float(venue_priors.fee_for(broker, taker=True)) if venue_priors else 0.0
            except Exception:  # noqa: BLE001
                fee_bps = 0.0
            try:
                spread_bps = float(venue_priors.spread_for(broker, ac)) if venue_priors else 0.0
            except Exception:  # noqa: BLE001
                spread_bps = 0.0
            try:
                slip_bps = float(slippage_model.estimate(
                    broker=broker, symbol="*", asset_class=ac,
                ).bps) if slippage_model else 0.0
            except Exception:  # noqa: BLE001
                slip_bps = 0.0
            samples.append(fee_bps + spread_bps + slip_bps)
    if not samples:
        return None
    return sum(samples) / len(samples)
