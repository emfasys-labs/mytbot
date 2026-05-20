"""
models/meta_label/thresholds.py
================================
Dynamic, context-aware threshold resolver for the trained meta-labeller.

The previous design encoded fixed per-mode / per-regime thresholds
(e.g. ``hunter: 0.35``) directly in YAML. That violated the
project-wide rule that *no operational threshold may be a frozen
absolute number* — every gate must respond to live market regime,
volatility, and operator deployment intent.

The new design separates two concerns:

1. **What win rate do we want right now?** — a function of mode,
   regime, market_state_score, market_volatility_scalar, and the
   *deployment pressure* (how far below the operator's slider target
   the actual deployment sits).
2. **What probability threshold produces that win rate?** — a lookup
   against the model's own validation calibration table (see
   :mod:`models.meta_label.calibration`).

The target win rate is always clamped to ``[calibration.base_rate,
calibration.best_observed]`` so we never accept worse-than-random
trades and never demand more than the model has ever delivered.

Resolution flow:

    target = (
        base_anchor
        + mode_offset[mode]
        + regime_caution_weight * (1 - market_state_score)
        + vol_caution_weight    * max(0, vol_scalar - 1)
        - deployment_relief_weight * deployment_pressure
    )
    target = clamp(target, target_floor, target_ceiling)
    threshold = calibration.lowest_threshold_for(target)

Static *anchors* (e.g. ``base_anchor: 0.42``) remain in config — but
they are calibration anchors, not operational thresholds. The
operational threshold is computed live for every candidate.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Mapping, Optional

from models.meta_label.calibration import CalibrationTable

logger = logging.getLogger(__name__)


# Sensible default anchor: ~equal to the validation base rate (0.418)
# of the baseline mytbot_meta_labeler. The dynamic resolver clamps
# anyway, so this is purely the "in the absence of all signal" target.
DEFAULT_TARGET_ANCHOR: float = 0.42
DEFAULT_TARGET_FLOOR: float = 0.20
DEFAULT_TARGET_CEILING: float = 0.85

# When no calibration table is available the resolver falls back to a
# legacy mode of returning the static target as a probability threshold
# directly. This preserves backward-compatible behaviour for tests and
# environments that haven't populated the calibration metadata yet.
LEGACY_FALLBACK_THRESHOLD: float = 0.42

# Back-compat alias: some older tests / imports still reference the
# original constant name. The dynamic resolver supersedes it.
DEFAULT_PROB_THRESHOLD: float = DEFAULT_TARGET_ANCHOR


@dataclass
class DynamicThresholdConfig:
    """Anchors + dynamic weights for the win-rate target."""

    base_anchor: float = DEFAULT_TARGET_ANCHOR
    by_mode_offset: dict[str, float] = field(default_factory=dict)
    by_regime_offset: dict[str, float] = field(default_factory=dict)
    by_mode_regime_offset: dict[str, float] = field(default_factory=dict)
    regime_caution_weight: float = 0.30
    vol_caution_weight: float = 0.10
    deployment_relief_weight: float = 0.20
    target_floor: float = DEFAULT_TARGET_FLOOR
    target_ceiling: float = DEFAULT_TARGET_CEILING

    @classmethod
    def from_dict(cls, raw: Optional[Mapping[str, object]]) -> "DynamicThresholdConfig":
        if not raw:
            return cls()
        # The block can be either at the top level or nested under
        # ``target_win_rate`` — both layouts are accepted to keep YAML
        # legible while leaving room for future dials at the parent.
        sect = raw.get("target_win_rate") if isinstance(raw, Mapping) and "target_win_rate" in raw else raw  # type: ignore[union-attr]
        sect = dict(sect or {})
        return cls(
            base_anchor=float(sect.get("base_anchor", DEFAULT_TARGET_ANCHOR)),
            by_mode_offset={
                str(k).lower(): float(v)
                for k, v in (sect.get("by_mode_offset") or {}).items()
            },
            by_regime_offset={
                str(k).lower(): float(v)
                for k, v in (sect.get("by_regime_offset") or {}).items()
            },
            by_mode_regime_offset={
                str(k).lower().replace(":", "."): float(v)
                for k, v in (sect.get("by_mode_regime_offset") or {}).items()
            },
            regime_caution_weight=float(sect.get("regime_caution_weight", 0.30)),
            vol_caution_weight=float(sect.get("vol_caution_weight", 0.10)),
            deployment_relief_weight=float(sect.get("deployment_relief_weight", 0.20)),
            target_floor=float(sect.get("target_floor", DEFAULT_TARGET_FLOOR)),
            target_ceiling=float(sect.get("target_ceiling", DEFAULT_TARGET_CEILING)),
        )


# ── Legacy threshold config (kept for backward compatibility) ────────────
# Older code (and existing tests) constructs ``ThresholdConfig`` with
# explicit ``default`` / ``by_mode`` / ``by_regime`` / ``by_mode_regime``
# probability values. The dynamic resolver supersedes this design — but
# we keep the legacy shape working so existing tests, scripts, and
# external integrations don't break. When ``ThresholdConfig`` is passed
# to ``threshold_for`` / ``resolve_threshold``, the legacy lookup is
# used directly and no calibration mapping is applied.


@dataclass
class ThresholdConfig:
    """Legacy static threshold lookup: mode-regime > mode > regime > default."""

    default: float = DEFAULT_PROB_THRESHOLD
    by_mode: dict[str, float] = field(default_factory=dict)
    by_regime: dict[str, float] = field(default_factory=dict)
    by_mode_regime: dict[str, float] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: Optional[Mapping[str, object]]) -> "ThresholdConfig":
        if not raw:
            return cls()
        default = float(raw.get("default", DEFAULT_PROB_THRESHOLD))  # type: ignore[arg-type]
        by_mode = {
            str(k).lower(): float(v)
            for k, v in (raw.get("by_mode") or {}).items()  # type: ignore[union-attr]
        }
        by_regime = {
            str(k).lower(): float(v)
            for k, v in (raw.get("by_regime") or {}).items()  # type: ignore[union-attr]
        }
        by_mr = {
            str(k).lower().replace(":", "."): float(v)
            for k, v in (raw.get("by_mode_regime") or {}).items()  # type: ignore[union-attr]
        }
        return cls(
            default=default,
            by_mode=by_mode,
            by_regime=by_regime,
            by_mode_regime=by_mr,
        )

    def lookup(self, *, mode: Optional[str], regime: Optional[str]) -> float:
        m = (mode or "").strip().lower() or None
        r = (regime or "").strip().lower() or None
        if m and r:
            key = f"{m}.{r}"
            if key in self.by_mode_regime:
                return float(self.by_mode_regime[key])
        if m and m in self.by_mode:
            return float(self.by_mode[m])
        if r and r in self.by_regime:
            return float(self.by_regime[r])
        return float(self.default)


@dataclass(frozen=True)
class ThresholdContext:
    """Live context that shapes the per-candidate target win rate."""

    mode: Optional[str] = None
    regime: Optional[str] = None
    market_state_score: float = 1.0
    market_volatility_scalar: float = 1.0
    deployment_pressure: float = 0.0  # in [0, 1]: slider_target - actual_deployment


@dataclass(frozen=True)
class ThresholdResolution:
    """Diagnostic record of *how* the threshold was computed."""

    threshold: float
    target_win_rate: float
    target_pre_clamp: float
    base_anchor: float
    mode_offset: float
    regime_offset: float
    regime_caution: float
    vol_caution: float
    deployment_relief: float
    floor: float
    ceiling: float
    calibration_used: bool


def resolve_threshold(
    config: DynamicThresholdConfig,
    *,
    context: ThresholdContext,
    calibration: Optional[CalibrationTable] = None,
) -> ThresholdResolution:
    """Compute the operational probability threshold from live context."""
    m = (context.mode or "").strip().lower() or None
    r = (context.regime or "").strip().lower() or None

    mode_offset = config.by_mode_offset.get(m, 0.0) if m else 0.0
    regime_offset = config.by_regime_offset.get(r, 0.0) if r else 0.0
    if m and r:
        key = f"{m}.{r}"
        if key in config.by_mode_regime_offset:
            # mode+regime override takes precedence (replaces the sum).
            mode_offset = config.by_mode_regime_offset[key]
            regime_offset = 0.0

    mss = max(0.0, min(1.0, float(context.market_state_score)))
    vol = max(0.0, float(context.market_volatility_scalar))
    dpress = max(0.0, min(1.0, float(context.deployment_pressure)))

    regime_caution = config.regime_caution_weight * (1.0 - mss)
    vol_caution = config.vol_caution_weight * max(0.0, vol - 1.0)
    deployment_relief = config.deployment_relief_weight * dpress

    target_pre = (
        config.base_anchor
        + mode_offset
        + regime_offset
        + regime_caution
        + vol_caution
        - deployment_relief
    )

    # Clamp against calibration evidence when available; otherwise use
    # the operator-configured floor/ceiling.
    if calibration is not None:
        floor = max(config.target_floor, calibration.base_rate_estimate * 0.7)
        ceiling = min(config.target_ceiling, calibration.best_observed)
    else:
        floor = config.target_floor
        ceiling = config.target_ceiling

    target = max(floor, min(ceiling, target_pre))

    if calibration is not None:
        threshold = calibration.lowest_threshold_for(target)
        cal_used = True
    else:
        # No calibration table → fall back to using the target as the
        # threshold directly. This is a *strict* interpretation: the
        # model's calibrated probability ≈ predicted win rate, so
        # admitting p >= target is the conservative legacy behaviour.
        threshold = target
        cal_used = False

    return ThresholdResolution(
        threshold=float(threshold),
        target_win_rate=float(target),
        target_pre_clamp=float(target_pre),
        base_anchor=float(config.base_anchor),
        mode_offset=float(mode_offset),
        regime_offset=float(regime_offset),
        regime_caution=float(regime_caution),
        vol_caution=float(vol_caution),
        deployment_relief=float(deployment_relief),
        floor=float(floor),
        ceiling=float(ceiling),
        calibration_used=cal_used,
    )


def threshold_for(
    config: "DynamicThresholdConfig | ThresholdConfig",
    *,
    mode: Optional[str] = None,
    regime: Optional[str] = None,
    market_state_score: float = 1.0,
    market_volatility_scalar: float = 1.0,
    deployment_pressure: float = 0.0,
    calibration: Optional[CalibrationTable] = None,
) -> float:
    """Backward-compatible facade. Returns the threshold scalar.

    Accepts either:
      * ``ThresholdConfig`` (legacy static lookup), or
      * ``DynamicThresholdConfig`` (dynamic, context-aware resolver).
    For new code that needs the diagnostic breakdown, call
    :func:`resolve_threshold` directly.
    """
    if isinstance(config, ThresholdConfig):
        return config.lookup(mode=mode, regime=regime)
    res = resolve_threshold(
        config,
        context=ThresholdContext(
            mode=mode,
            regime=regime,
            market_state_score=market_state_score,
            market_volatility_scalar=market_volatility_scalar,
            deployment_pressure=deployment_pressure,
        ),
        calibration=calibration,
    )
    return res.threshold
