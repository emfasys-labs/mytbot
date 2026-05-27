"""
system/adaptive_regime_weights.py
==================================
Strategy × market-state opportunity multipliers — **computed live from
continuous market features**, not looked up from a stored table.

Why this is different from the static-table version:

The old design stored ``mean_reversion in trend_up = 0.70`` as a fixed
value. That's the same fade whether the trend is barely there
(trend_strength=0.1) or absolutely overwhelming (trend_strength=0.9).
The 2026-05-26 audit traced an $8K bleed to mean-reversion shorting an
"undeclared" trend — i.e. a regime the discrete classifier labelled
"mixed" while equities ground steadily higher.

The new design replaces the look-up with a formula:

    score      = Σ over features:  affinity_sign * feature_value
    multiplier = 1 + sensitivity * tanh(score)
    clamped to [bounds.min, bounds.max] from YAML

Where:
  * ``feature_value`` is the live continuous value (trend_strength,
    chaos_penalty, volatility_structure, …) from ``RegimeState`` —
    these already update every regime tick.
  * ``affinity_sign`` is a categorical descriptor of each strategy's
    nature: ``aligned`` (+1), ``opposed`` (-1), ``neutral`` (0). These
    capture WHAT each strategy needs to thrive — they are design
    statements about the strategy's edge, not tuning constants. A
    later iteration can replace these with weights learned from rolling
    realised P&L per market state.
  * ``sensitivity`` and ``bounds`` are YAML-driven safety knobs in
    ``config/strategies.yaml::regime_weights``. The function refuses
    to invent defaults — missing config → multiplier = 1.0 for every
    input (no-op).

Result: the multiplier varies CONTINUOUSLY with market conditions.
Mean-reversion's fade in a weak trend is mild; in a strong trend it's
heavy — automatically, without any threshold flip or stored value.
"""

from __future__ import annotations

import math
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import yaml

# Categorical descriptors of strategy nature. These are NOT tuning
# constants — they answer "what kind of market does this strategy want?"
# in three categorical buckets:
#   aligned  → the strategy's edge increases when this feature is high
#   opposed  → the strategy's edge decreases when this feature is high
#   neutral  → this feature does not change the strategy's edge
#
# Adding a new strategy requires one new row; adding a new market feature
# requires one new column on each strategy that responds to it. No numbers
# anywhere — only categorical relationships.
ALIGNED = "aligned"
OPPOSED = "opposed"
NEUTRAL = "neutral"

# Feature names mirror the keys produced by ``risk/regime_state.py`` in
# ``RegimeState.components`` so the live regime tick can feed them
# straight in.
_STRATEGY_AFFINITY: dict[str, dict[str, str]] = {
    # Mean-reversion: hates trends, hates chaos, mildly likes vol
    # (gives it bounded oscillation to fade) and liquidity (to enter/exit).
    "mean_reversion": {
        "trend_strength":      OPPOSED,
        "chaos_penalty":       OPPOSED,
        "correlation_crowding": OPPOSED,
        "volatility_structure": ALIGNED,
        "liquidity_state":     ALIGNED,
    },
    # Momentum breakout: loves trends and confirming cross-asset flow.
    # Dies in chaotic, low-conviction tape.
    "momentum_breakout": {
        "trend_strength":         ALIGNED,
        "cross_asset_confirmation": ALIGNED,
        "risk_on_breadth":        ALIGNED,
        "chaos_penalty":          OPPOSED,
        "news_conflict_score":    OPPOSED,
    },
    # Volume-flow: similar to momentum but more about flow-confirmation
    # than pure price-breakout, so it cares more about breadth.
    "volume_flow": {
        "trend_strength":         ALIGNED,
        "risk_on_breadth":        ALIGNED,
        "cross_asset_confirmation": ALIGNED,
        "chaos_penalty":          OPPOSED,
        "anomaly_breadth":        ALIGNED,
    },
    # Volatility-regime strategy: built for vol structure changes.
    # Loves vol, loves anomaly bursts; fades in calm trends.
    "volatility_regime": {
        "volatility_structure": ALIGNED,
        "anomaly_breadth":      ALIGNED,
        "chaos_penalty":        ALIGNED,   # chaos = its hunting ground
        "trend_strength":       OPPOSED,
    },
    # Event-driven news: amplifies in stress and confusion (asymmetric info).
    "event_driven_news": {
        "news_conflict_score": ALIGNED,
        "chaos_penalty":       ALIGNED,
        "volatility_structure": ALIGNED,
        "macro_clarity":       OPPOSED,    # everyone agreeing dampens news edge
    },
    # Pairs trading: needs stable cointegrated relationships and
    # mean-reverting spread behaviour — opposite of momentum. We
    # intentionally do NOT mark cross_asset_confirmation as aligned
    # here: high cross-asset confirm in a trending market is a false
    # positive for pairs (both legs moving together AND directionally),
    # so the trend_strength penalty has to dominate.
    "pairs_trading": {
        "trend_strength":       OPPOSED,
        "correlation_crowding": ALIGNED,
        "chaos_penalty":        OPPOSED,
        "liquidity_state":      ALIGNED,
    },
    # Regime rotation: thrives when trend + breadth confirm the rotation
    # narrative.
    "regime_rotation": {
        "trend_strength":   ALIGNED,
        "risk_on_breadth":  ALIGNED,
        "macro_clarity":    ALIGNED,
        "chaos_penalty":    OPPOSED,
    },
}

_AFFINITY_VALUES = {ALIGNED: 1, OPPOSED: -1, NEUTRAL: 0}

# Light backwards-compatibility layer: when callers still pass a discrete
# regime label (the legacy API used by ``opportunity_engine.py`` and the
# regression tests), synthesise a feature dict that captures the meaning
# of that label. The synthesis is a categorical description (1.0 on the
# features the label implies, 0.0 elsewhere) — no tuning involved.
_LABEL_TO_FEATURES: dict[str, dict[str, float]] = {
    "trend_up":   {"trend_strength": 1.0, "risk_on_breadth": 1.0, "cross_asset_confirmation": 1.0},
    "trend_down": {"trend_strength": 1.0, "risk_on_breadth": 0.0, "cross_asset_confirmation": 1.0},
    "range":      {"trend_strength": 0.0, "volatility_structure": 0.2, "liquidity_state": 0.8},
    "volatile":   {"volatility_structure": 1.0, "anomaly_breadth": 0.7},
    "crash":      {"chaos_penalty": 1.0, "anomaly_breadth": 1.0, "trend_strength": 0.8, "macro_clarity": 0.0},
    "panic":      {"chaos_penalty": 1.0, "news_conflict_score": 1.0, "volatility_structure": 1.0},
    "risk_on":    {"risk_on_breadth": 1.0, "cross_asset_confirmation": 0.8, "trend_strength": 0.5},
    "risk_off":   {"risk_on_breadth": 0.0, "chaos_penalty": 0.5, "trend_strength": 0.5},
    # "mixed" deliberately leaves every feature at its midpoint so the
    # label-based path returns the neutral multiplier (1.0). When the
    # market is genuinely mixed the LIVE-features path (passing the
    # actual RegimeState.components) is what should drive multipliers
    # — and that path does the right thing tick-by-tick.
    "mixed":      {"trend_strength": 0.5, "chaos_penalty": 0.5, "macro_clarity": 0.5},
}


_CONFIG_PATH = Path("config/strategies.yaml")
_NEUTRAL_MULT = Decimal("1.0")
_cache: tuple[float, dict[str, Any] | None] | None = None


def _load_block() -> dict[str, Any] | None:
    """Return the live ``regime_weights`` block, or ``None`` if absent/disabled."""
    global _cache
    try:
        mtime = _CONFIG_PATH.stat().st_mtime
    except OSError:
        _cache = None
        return None
    if _cache is not None and _cache[0] == mtime:
        return _cache[1]
    try:
        with _CONFIG_PATH.open(encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except Exception:  # noqa: BLE001
        _cache = (mtime, None)
        return None
    block = data.get("regime_weights")
    if not isinstance(block, dict) or not bool(block.get("enabled", False)):
        _cache = (mtime, None)
        return None
    _cache = (mtime, block)
    return block


def _yaml_decimal(block: dict[str, Any], key: str) -> Decimal | None:
    raw = block.get(key)
    if raw is None:
        return None
    try:
        return Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _yaml_bounds(block: dict[str, Any]) -> tuple[Decimal, Decimal] | None:
    bounds = block.get("bounds")
    if not isinstance(bounds, dict):
        return None
    lo_raw = bounds.get("min")
    hi_raw = bounds.get("max")
    if lo_raw is None or hi_raw is None:
        return None
    try:
        lo = Decimal(str(lo_raw))
        hi = Decimal(str(hi_raw))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if lo > hi or lo <= 0:
        return None
    return (lo, hi)


def compute_multiplier(
    strategy_name: str,
    market_features: dict[str, Any] | None,
) -> Decimal:
    """Compute the live opportunity-score multiplier for ``strategy_name``.

    ``market_features`` is a dict of {feature_name: feature_value} taken
    straight from ``RegimeState.components`` (or any equivalent live
    feature source). Feature values should be normalised to roughly
    [0, 1] — the formula tolerates out-of-range inputs via ``tanh`` and
    final clamping, but feature normalisation upstream is preferred.

    Returns ``Decimal("1.0")`` whenever:
      * YAML ``regime_weights`` block is absent or disabled
      * ``sensitivity`` / ``bounds`` are missing or invalid in YAML
      * the strategy has no affinity row (unknown strategy)
      * no live features overlap with the strategy's affinity row

    No tuning defaults are invented in code.
    """
    if not strategy_name:
        return _NEUTRAL_MULT
    block = _load_block()
    if block is None:
        return _NEUTRAL_MULT
    bounds = _yaml_bounds(block)
    if bounds is None:
        return _NEUTRAL_MULT
    sensitivity = _yaml_decimal(block, "sensitivity")
    if sensitivity is None or sensitivity < 0:
        return _NEUTRAL_MULT
    affinity = _STRATEGY_AFFINITY.get(str(strategy_name).strip().lower())
    if not affinity:
        return _NEUTRAL_MULT
    features = market_features if isinstance(market_features, dict) else {}

    # Compute the alignment score across whichever features are present.
    # Features are expected to be normalised to [0, 1]; we rescale to
    # [-1, +1] so the midpoint (0.5) is the neutral point. Below the
    # midpoint, the feature is "anti-aligned" with its semantic — e.g.
    # trend_strength = 0.0 means "no trend / range" which strongly
    # boosts mean-reversion AND fades momentum, because mean_reversion
    # is OPPOSED to trend (sign=-1) and -1 × -1 = +1.
    # The rescale is a mathematical centering, not a tuning constant.
    score = 0.0
    overlap = 0
    for feat_name, label in affinity.items():
        if feat_name not in features:
            continue
        try:
            feat_val = float(features[feat_name])
        except (TypeError, ValueError):
            continue
        sign = _AFFINITY_VALUES.get(label, 0)
        if sign == 0:
            continue
        # Rescale [0, 1] → [-1, +1] (clamp to range first).
        clipped = max(0.0, min(1.0, feat_val))
        centred = 2.0 * clipped - 1.0
        score += sign * centred
        overlap += 1

    if overlap == 0:
        return _NEUTRAL_MULT

    # tanh keeps the score bounded in (-1, 1) before sensitivity scaling,
    # so multiplier is naturally bounded in (1 - sensitivity, 1 + sensitivity).
    # Then we clamp to the YAML safety band.
    bounded = math.tanh(score)
    raw_mult = Decimal("1") + sensitivity * Decimal(str(bounded))
    lo, hi = bounds
    if raw_mult < lo:
        return lo
    if raw_mult > hi:
        return hi
    return raw_mult


def strategy_regime_multiplier(strategy_name: str, regime_label: str) -> Decimal:
    """Backwards-compatible label-based API.

    Translates the discrete ``regime_label`` into a synthetic feature
    dict and calls :func:`compute_multiplier`. New code should call
    :func:`compute_multiplier` directly with the live ``RegimeState``
    components so the multiplier varies smoothly with feature intensity
    instead of jumping between discrete labels.
    """
    if not strategy_name or not regime_label:
        return _NEUTRAL_MULT
    features = _LABEL_TO_FEATURES.get(str(regime_label).strip().lower())
    if features is None:
        return _NEUTRAL_MULT
    return compute_multiplier(strategy_name, features)
