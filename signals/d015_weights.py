"""Resolve profile mode coefficients with regime features (D015 dynamic weights)."""

from __future__ import annotations

from decimal import Decimal

from config.models import CoefficientWithDynamic, ProfileModesConfig
from core.models_runtime import ProfileMode, RegimeState, clip_decimal


def _d(x: float) -> Decimal:
    return Decimal(str(x))


def resolve_coefficient(coeff: CoefficientWithDynamic, regime: RegimeState) -> Decimal:
    """
    effective = base + sum(dynamic_key_weight * matching_regime_component).

    Dynamic keys ending in ``_weight`` multiply the regime field with the same prefix
    (e.g. ``trend_strength_weight`` * ``regime.components.trend_strength``).
    """
    total = _d(coeff.base)
    comp = regime.components
    field_map = {
        "trend_strength": comp.trend_strength,
        "breadth": regime.breadth_score,
        "drawdown_penalty": Decimal("1") - regime.drawdown_throttle,
        "chaos_penalty": comp.chaos_penalty,
        "best_opportunity": regime.breadth_score,
        "crowding_penalty": comp.correlation_crowding,
        "liquidity_support": comp.liquidity_state,
        "urgency": regime.breadth_score,
        "switching_cost": comp.liquidity_state,
        "opportunity": regime.breadth_score,
        "market_state": clip_decimal(regime.market_state_score, Decimal("-1"), Decimal("1")),
        "liquidity": comp.liquidity_state,
        "persistence": comp.anomaly_breadth,
        "fake_spike_penalty": comp.chaos_penalty,
        "spread": comp.liquidity_state,
        "depth_fragility": comp.chaos_penalty,
        "slippage": comp.liquidity_state,
        "vol_shock": comp.volatility_structure,
        "correlation_break": comp.correlation_crowding,
        "news_conflict": comp.news_conflict_score,
        "reversal": comp.chaos_penalty,
        "volume_fade": comp.liquidity_state,
        "opportunity_cost": comp.anomaly_breadth,
        "daily_drawdown": Decimal("1") - regime.drawdown_throttle,
        "rolling_drawdown": Decimal("1") - regime.drawdown_throttle,
        "loss_streak": comp.chaos_penalty,
    }
    for key, w in coeff.dynamic.items():
        if not key.endswith("_weight"):
            continue
        prefix = key[: -len("_weight")]
        if prefix in field_map:
            total += _d(w) * field_map[prefix]
    return clip_decimal(total, Decimal("0.05"), Decimal("4"))


def volume_anomaly_weight_for_mode(
    profile: ProfileModesConfig,
    mode: ProfileMode,
    regime: RegimeState,
) -> Decimal:
    c = profile.modes[mode].coefficients.volume_anomaly_weight
    return resolve_coefficient(c, regime)


def concentration_exponent_for_mode(profile: ProfileModesConfig, mode: ProfileMode, regime: RegimeState) -> Decimal:
    c = profile.modes[mode].coefficients.concentration_exponent
    return resolve_coefficient(c, regime)


def aggression_multiplier_for_mode(profile: ProfileModesConfig, mode: ProfileMode, regime: RegimeState) -> Decimal:
    c = profile.modes[mode].coefficients.aggression_multiplier
    return resolve_coefficient(c, regime)


def replacement_sensitivity_for_mode(profile: ProfileModesConfig, mode: ProfileMode, regime: RegimeState) -> Decimal:
    c = profile.modes[mode].coefficients.replacement_sensitivity
    return resolve_coefficient(c, regime)
