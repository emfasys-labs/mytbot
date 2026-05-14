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

    Heuristic:
      * win_rate > 0.55 AND avg_ret > 0 → shrink cushion to 0.85
        (we're winning; let more setups through)
      * win_rate < 0.45 OR avg_ret < 0 → expand to 1.4
        (we're bleeding; raise the bar)
      * otherwise → 1.0

    Returns 1.0 on missing inputs (fresh boot has no statistics yet).
    """
    if win_rate is None or avg_ret is None:
        return 1.0
    if win_rate >= 0.55 and avg_ret > 0:
        return 0.85
    if win_rate < 0.45 or avg_ret < 0:
        return 1.4
    return 1.0


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
