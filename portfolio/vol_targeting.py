"""
portfolio/vol_targeting.py
============================
Wave 8 — volatility targeting + drawdown-aware scaling.

Pure numeric helpers. Callers (the allocator, sizing layer) supply
realised stats and read back a scalar in ``[min_scale, max_scale]``
that they then apply to gross exposure or per-position notional.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional


@dataclass
class ScaleResult:
    """Combined scale + the components that produced it (for the dashboard)."""

    scale: float
    vol_component: Optional[float] = None
    drawdown_component: Optional[float] = None


def vol_scale(
    *,
    target_vol: float,
    realised_vol: float,
    min_scale: float = 0.25,
    max_scale: float = 2.0,
) -> float:
    """
    ``scale = target_vol / realised_vol``, clipped to
    ``[min_scale, max_scale]``. When ``realised_vol`` is zero or
    non-finite, returns ``1.0`` (no scaling) so the caller falls back to
    its baseline sizing.
    """
    if realised_vol is None or not math.isfinite(realised_vol) or realised_vol <= 0:
        return 1.0
    if target_vol is None or not math.isfinite(target_vol) or target_vol <= 0:
        return 1.0
    s = float(target_vol) / float(realised_vol)
    return max(min_scale, min(max_scale, s))


def drawdown_scale(
    *,
    drawdown: float,
    soft_drawdown: float = 0.05,
    hard_drawdown: float = 0.20,
    floor: float = 0.10,
) -> float:
    """
    Linear scale-down from 1.0 → ``floor`` as drawdown moves from
    ``soft_drawdown`` to ``hard_drawdown``. Drawdowns past the hard
    threshold clamp at ``floor``.

    ``drawdown`` is a positive number representing magnitude (e.g. 0.07
    means -7%).
    """
    dd = max(0.0, float(drawdown))
    if dd <= soft_drawdown:
        return 1.0
    if dd >= hard_drawdown:
        return floor
    span = max(1e-12, hard_drawdown - soft_drawdown)
    ratio = (dd - soft_drawdown) / span
    return float(1.0 - ratio * (1.0 - floor))


def combined_scale(
    *,
    target_vol: Optional[float] = None,
    realised_vol: Optional[float] = None,
    drawdown: Optional[float] = None,
    soft_drawdown: float = 0.05,
    hard_drawdown: float = 0.20,
    floor: float = 0.10,
    min_scale: float = 0.25,
    max_scale: float = 2.0,
) -> ScaleResult:
    """
    Multiplicative blend of vol-targeting and drawdown-aware scales.

    Either component is optional. When both are provided, the final
    scale is ``vol_scale * drawdown_scale`` — vol scaling can lift
    exposure but a deep drawdown forces it back down.
    """
    vc: Optional[float] = None
    dc: Optional[float] = None
    if target_vol is not None and realised_vol is not None:
        vc = vol_scale(
            target_vol=target_vol,
            realised_vol=realised_vol,
            min_scale=min_scale,
            max_scale=max_scale,
        )
    if drawdown is not None:
        dc = drawdown_scale(
            drawdown=drawdown,
            soft_drawdown=soft_drawdown,
            hard_drawdown=hard_drawdown,
            floor=floor,
        )
    if vc is None and dc is None:
        return ScaleResult(scale=1.0)
    if vc is None:
        return ScaleResult(scale=dc, vol_component=None, drawdown_component=dc)
    if dc is None:
        return ScaleResult(scale=vc, vol_component=vc, drawdown_component=None)
    return ScaleResult(scale=vc * dc, vol_component=vc, drawdown_component=dc)
