"""
Bounded transforms for D015 scoring (Decimal in/out). Float used only inside exp/tanh.
"""

from __future__ import annotations

import math
from decimal import Decimal

from core.models_runtime import clip_decimal


def normalize_zscore(z: Decimal, clip_lo: Decimal = Decimal("-4"), clip_hi: Decimal = Decimal("4")) -> Decimal:
    """Map clipped z-score to [0, 1] linearly."""
    c = clip_decimal(z, clip_lo, clip_hi)
    return (c - clip_lo) / (clip_hi - clip_lo)


def tanh_clip(x: Decimal) -> Decimal:
    """tanh(float(x)) as Decimal, bounded."""
    return Decimal(str(math.tanh(float(x))))


def bounded_sigmoid(x: Decimal) -> Decimal:
    """1 / (1 + exp(-x)) with x clipped for stability."""
    xf = float(x)
    xc = max(-20.0, min(20.0, xf))
    return Decimal(str(1.0 / (1.0 + math.exp(-xc))))
