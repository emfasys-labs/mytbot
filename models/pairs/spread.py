"""
models/pairs/spread.py
========================
Wave 5 — spread mathematics for pairs trading.

A "pair" is two price series ``(y, x)`` and a hedge ratio ``β`` such
that the spread ``s_t = y_t - β * x_t`` is (hopefully) mean-reverting.
This module computes the spread, its rolling z-score, and the
Ornstein-Uhlenbeck half-life — the canonical inputs to every
relative-value strategy.

All functions are pure, NumPy-only, and leakage-safe (no future bars
leak into the current row).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd


# ── spread + z-score ────────────────────────────────────────────────────────


def compute_spread(
    y: pd.Series,
    x: pd.Series,
    *,
    beta: float | pd.Series,
    intercept: float | pd.Series = 0.0,
) -> pd.Series:
    """
    ``s_t = y_t - β_t * x_t - α_t``

    ``beta`` and ``intercept`` may be scalars or Series aligned to
    ``y.index``. The output preserves ``y``'s index.
    """
    if not y.index.equals(x.index):
        x = x.reindex(y.index)
    if isinstance(beta, pd.Series):
        b = beta.reindex(y.index).astype(float)
    else:
        b = float(beta)
    if isinstance(intercept, pd.Series):
        a = intercept.reindex(y.index).astype(float)
    else:
        a = float(intercept)
    return (y.astype(float) - b * x.astype(float) - a).astype(float)


def spread_zscore(
    spread: pd.Series,
    *,
    window: int = 60,
    min_periods: Optional[int] = None,
) -> pd.Series:
    """
    Rolling z-score of the spread.

    Uses a *trailing* window so row ``t`` uses only ``[t - window, t]``
    — no leakage. The first ``min_periods - 1`` rows are NaN.
    """
    if window <= 1:
        raise ValueError("window must be >= 2")
    mp = window if min_periods is None else int(min_periods)
    mu = spread.rolling(window, min_periods=mp).mean()
    sd = spread.rolling(window, min_periods=mp).std(ddof=1)
    z = (spread - mu) / sd.replace(0.0, np.nan)
    return z


# ── OU half-life ────────────────────────────────────────────────────────────


def half_life_ou(spread: pd.Series) -> Optional[float]:
    """
    Estimate the Ornstein-Uhlenbeck half-life of mean reversion.

    Model: ``Δs_t = κ * (μ - s_{t-1}) + ε_t``
    Equivalent regression: ``Δs_t = c + φ * s_{t-1} + ε_t`` where
    ``φ = -κ`` and the half-life is ``log(2) / κ = log(2) / -φ``.

    Returns ``None`` when the series is too short, when the regression
    is degenerate, or when the estimated κ is non-positive (indicating
    the series is *not* mean-reverting at the chosen frequency).
    """
    s = spread.dropna().astype(float)
    if len(s) < 30:
        return None
    delta = s.diff().iloc[1:].to_numpy()
    lag = s.shift(1).iloc[1:].to_numpy()
    if not np.all(np.isfinite(delta)) or not np.all(np.isfinite(lag)):
        return None
    n = len(delta)
    X = np.column_stack([np.ones(n), lag])
    try:
        coef, *_ = np.linalg.lstsq(X, delta, rcond=None)
    except np.linalg.LinAlgError:
        return None
    phi = float(coef[1])
    if phi >= 0 or not math.isfinite(phi):
        return None
    kappa = -phi
    return float(math.log(2.0) / kappa)
