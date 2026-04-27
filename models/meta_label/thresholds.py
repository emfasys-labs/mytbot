"""
models/meta_label/thresholds.py
================================
Wave 2 — per-mode / per-regime probability thresholds.

The trained meta-labeller emits a calibrated probability ``p`` that a
candidate trade reaches the profit barrier before the stop barrier.
Sizing and gating require a threshold; defaults are deliberately
conservative ("when in doubt, skip") and operator-tunable via
``config/meta_labeler.yaml``.

The threshold lookup precedence is:

    1. (mode, regime) pair, if both are set and present.
    2. mode override.
    3. regime override.
    4. ``DEFAULT_PROB_THRESHOLD`` from this module.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Optional


# Conservative default — must be > 0.5 to be informative. 0.55 matches
# the heuristic baseline in ``signals/meta_labeler.py`` so behaviour is
# continuous when an operator first enables the trained gate.
DEFAULT_PROB_THRESHOLD: float = 0.55


@dataclass
class ThresholdConfig:
    """
    Container for per-mode / per-regime threshold overrides.

    Example YAML::

        thresholds:
          default: 0.55
          by_mode:
            defender: 0.62
            trader:   0.55
            hunter:   0.50
          by_regime:
            crash:    0.70
            volatile: 0.60
          by_mode_regime:
            hunter.crash: 0.65
    """

    default: float = DEFAULT_PROB_THRESHOLD
    by_mode: dict[str, float] = field(default_factory=dict)
    by_regime: dict[str, float] = field(default_factory=dict)
    by_mode_regime: dict[str, float] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: Optional[Mapping[str, object]]) -> "ThresholdConfig":
        if not raw:
            return cls()
        default = float(raw.get("default", DEFAULT_PROB_THRESHOLD))  # type: ignore[arg-type]
        by_mode = {str(k).lower(): float(v) for k, v in (raw.get("by_mode") or {}).items()}  # type: ignore[union-attr]
        by_regime = {str(k).lower(): float(v) for k, v in (raw.get("by_regime") or {}).items()}  # type: ignore[union-attr]
        by_mr = {
            str(k).lower(): float(v)
            for k, v in (raw.get("by_mode_regime") or {}).items()  # type: ignore[union-attr]
        }
        # Allow both "hunter.crash" and "hunter:crash" notations.
        by_mr = {key.replace(":", "."): val for key, val in by_mr.items()}
        return cls(
            default=default,
            by_mode=by_mode,
            by_regime=by_regime,
            by_mode_regime=by_mr,
        )


def threshold_for(
    config: ThresholdConfig,
    *,
    mode: Optional[str] = None,
    regime: Optional[str] = None,
) -> float:
    """Resolve the threshold for the given (mode, regime) pair."""
    m = (mode or "").strip().lower() or None
    r = (regime or "").strip().lower() or None

    if m and r:
        key = f"{m}.{r}"
        if key in config.by_mode_regime:
            return float(config.by_mode_regime[key])

    if m and m in config.by_mode:
        return float(config.by_mode[m])

    if r and r in config.by_regime:
        return float(config.by_regime[r])

    return float(config.default)
