"""
portfolio/kelly.py
===================
Wave 8 — Kelly fractional sizing.

Single- and multi-asset versions. Half-Kelly is the default because
full-Kelly is famously fragile to estimation error in expected return.
Both functions clip output to a configurable hard cap and a safety
floor (from ``config/profile_modes.yaml`` when the caller plumbs it
through; the function itself is just numeric).

All math is float; the caller converts to ``Decimal`` for sizing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

import numpy as np


DEFAULT_HARD_CAP: float = 1.0  # never above 100% of capital
DEFAULT_FLOOR: float = 0.0     # never short via Kelly (callers manage shorts via signed return)


def kelly_fraction(
    expected_return: float,
    variance: float,
    *,
    half_kelly: bool = True,
    hard_cap: float = DEFAULT_HARD_CAP,
    floor: float = DEFAULT_FLOOR,
) -> float:
    """
    Single-asset Kelly. Returns the *signed* fraction of capital.

    f* = expected_return / variance  (full Kelly)
    half-Kelly multiplies by 0.5.

    Output is clipped to ``[-hard_cap, hard_cap]`` and additionally
    floored so the lower bound is at least ``floor`` (default 0 ⇒ no
    short via Kelly; pass ``floor=-hard_cap`` to allow shorts).
    """
    if variance is None or variance <= 0 or not np.isfinite(variance):
        return 0.0
    if expected_return is None or not np.isfinite(expected_return):
        return 0.0
    f = float(expected_return) / float(variance)
    if half_kelly:
        f *= 0.5
    f = max(floor, min(hard_cap, f))
    return float(f)


@dataclass
class KellySizingResult:
    weights: np.ndarray
    half_kelly: bool
    hard_cap: float
    floor: float
    raw_unscaled: np.ndarray  # Kelly weights before cap/floor clipping


def kelly_weights(
    expected_returns: Iterable[float],
    covariance: np.ndarray,
    *,
    half_kelly: bool = True,
    hard_cap: float = DEFAULT_HARD_CAP,
    floor: float = DEFAULT_FLOOR,
    ridge: float = 1e-6,
) -> KellySizingResult:
    """
    Multi-asset Kelly:

    f* = Σ⁻¹ μ      (full Kelly)

    A small ridge term is added to ``Σ`` for numerical stability —
    callers with well-conditioned covariance can pass ``ridge=0``.

    The result is clipped per-asset to ``[floor, hard_cap]`` AND scaled
    so the gross exposure does not exceed the hard cap (defensive: a
    naive sum of clipped weights can violate gross when N is large).
    """
    mu = np.asarray(list(expected_returns), dtype=float)
    cov = np.asarray(covariance, dtype=float)
    if mu.ndim != 1:
        raise ValueError("expected_returns must be 1-D")
    if cov.shape != (len(mu), len(mu)):
        raise ValueError("covariance shape must match expected_returns")
    p = len(mu)
    if p == 0:
        return KellySizingResult(
            weights=np.zeros(0), half_kelly=half_kelly,
            hard_cap=hard_cap, floor=floor, raw_unscaled=np.zeros(0),
        )

    A = cov + ridge * np.eye(p)
    try:
        raw = np.linalg.solve(A, mu)
    except np.linalg.LinAlgError:
        # Singular: fall back to inverse-variance weighting on diagonal.
        diag = np.maximum(np.diag(cov), 1e-12)
        raw = mu / diag

    if half_kelly:
        raw = raw * 0.5

    clipped = np.clip(raw, floor, hard_cap)

    # Gross-exposure cap: if Σ|w| > hard_cap, scale uniformly down.
    gross = float(np.sum(np.abs(clipped)))
    if gross > hard_cap > 0:
        clipped = clipped * (hard_cap / gross)

    return KellySizingResult(
        weights=clipped,
        half_kelly=half_kelly,
        hard_cap=hard_cap,
        floor=floor,
        raw_unscaled=raw,
    )


def cap_to_safety_bounds(
    weight: float,
    *,
    hard_cap: Optional[float] = None,
    floor: Optional[float] = None,
) -> float:
    """
    Operator-supplied hard cap / floor (e.g. from
    ``profile_modes.yaml`` ``safety_bounds``). Returns the clamped
    weight; pass ``None`` to skip a side.
    """
    w = float(weight)
    if floor is not None:
        w = max(w, float(floor))
    if hard_cap is not None:
        w = min(w, float(hard_cap))
    return w
