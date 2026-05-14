"""
system/adaptive_regime_weights.py
==================================
Map (strategy, regime) → opportunity-score multiplier.

Replaces (some of) the per-strategy ``mode_calibration`` blocks across
``config/strategies.yaml`` with a single, transparent table driven by
the live regime label from ``risk/regime_state.py``.

Why: strategies have natural alignments with market regimes. Momentum
makes money in trending markets and bleeds in ranges. Mean-reversion is
the opposite. The old fix was to switch the operator's mode and rely on
per-strategy thresholds inside YAML — opaque, mode-keyed, and didn't
follow live market state. Phase 4 multiplies each strategy's final
opportunity_score by a (strategy, regime) multiplier instead:

  multiplier > 1.0 → boost this strategy's signals in this regime
  multiplier < 1.0 → fade this strategy's signals in this regime
  multiplier = 1.0 → no preference

The multiplier never goes to zero — every strategy stays alive in every
regime. If we're confidently wrong about the regime, no strategy gets
silenced, only down-weighted.

The function is pure and falls back to 1.0 (no preference) for unknown
strategy / regime combos.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

# Mirror RegimeLabel from core.models_runtime; keeping this as a string
# rather than the Literal import so this module doesn't depend on the
# runtime models package (which has heavier transitive imports).
RegimeName = str


# Multiplier table — values are conservative (0.7–1.3) so a misclassified
# regime can't materially distort the strategy mix on a single tick.
# Rows = strategy, columns = regime. Anything missing → 1.0 (neutral).
_TABLE: dict[str, dict[str, float]] = {
    # Momentum / trend-following: love trends, hate ranges.
    "momentum_breakout": {
        "trend_up": 1.30,
        "trend_down": 1.30,
        "range": 0.75,
        "volatile": 0.90,
        "crash": 0.70,
        "panic": 0.60,
        "risk_on": 1.15,
        "risk_off": 0.90,
    },
    # Volume-flow has a similar trend bias but slightly less directional.
    "volume_flow": {
        "trend_up": 1.20,
        "trend_down": 1.20,
        "range": 0.85,
        "volatile": 0.95,
        "crash": 0.75,
        "panic": 0.65,
        "risk_on": 1.10,
        "risk_off": 0.95,
    },
    # Mean-reversion: opposite — thrives in chop and high-vol oscillations.
    "mean_reversion": {
        "trend_up": 0.70,
        "trend_down": 0.70,
        "range": 1.30,
        "volatile": 1.20,
        "crash": 0.80,  # still risky during crash
        "panic": 0.60,
        "risk_on": 0.95,
        "risk_off": 1.05,
    },
    # Volatility-regime strategy: explicitly designed for vol; loves
    # vol-rich regimes, fades calm ones.
    "volatility_regime": {
        "trend_up": 0.90,
        "trend_down": 0.95,
        "range": 0.90,
        "volatile": 1.30,
        "crash": 1.20,
        "panic": 1.10,
        "risk_on": 0.90,
        "risk_off": 1.05,
    },
    # Event-driven: news amplifies in stress, fades in calm bull markets.
    "event_driven_news": {
        "trend_up": 1.00,
        "trend_down": 1.05,
        "range": 1.00,
        "volatile": 1.20,
        "crash": 1.15,
        "panic": 1.10,
        "risk_on": 1.00,
        "risk_off": 1.10,
    },
    # Pairs trading and regime rotation: mean-revert-like in nature.
    "pairs_trading": {
        "trend_up": 0.80,
        "trend_down": 0.80,
        "range": 1.25,
        "volatile": 1.10,
        "crash": 0.85,
        "panic": 0.70,
        "risk_on": 1.00,
        "risk_off": 1.00,
    },
    "regime_rotation": {
        "trend_up": 1.10,
        "trend_down": 1.10,
        "range": 0.95,
        "volatile": 1.05,
        "crash": 1.00,
        "panic": 0.90,
        "risk_on": 1.10,
        "risk_off": 0.90,
    },
}


# Hard safety bounds — multiplier never goes outside this band, so a
# misconfigured row can't completely silence (or 10x) a strategy.
_MULT_MIN = 0.5
_MULT_MAX = 1.5


def strategy_regime_multiplier(strategy_name: str, regime_label: RegimeName) -> Decimal:
    """Return the opportunity-score multiplier for ``(strategy, regime)``.

    Unknown combos → ``Decimal("1.0")`` (no preference, signals pass
    through unchanged). Clamped to [_MULT_MIN, _MULT_MAX].
    """
    if not strategy_name or not regime_label:
        return Decimal("1.0")
    row = _TABLE.get(str(strategy_name).strip().lower())
    if not row:
        return Decimal("1.0")
    raw = row.get(str(regime_label).strip().lower())
    if raw is None:
        return Decimal("1.0")
    clamped = max(_MULT_MIN, min(_MULT_MAX, float(raw)))
    return Decimal(str(clamped))
