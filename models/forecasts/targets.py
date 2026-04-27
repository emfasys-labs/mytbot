"""
models/forecasts/targets.py
============================
Wave 6 — leakage-safe target builders for multi-horizon forecasting.

Every function here:

- consumes only future bars in ``close.iloc[i+1 : i+1+horizon]`` for row
  ``i``,
- emits ``NaN`` for the trailing rows whose horizon would overrun the
  series, so the training pipeline can drop them explicitly (it does
  in ``dataset.py``).

Supported targets:

- ``forward_return(horizon)`` → simple return over the next ``horizon`` bars.
- ``breakout_continuation`` → Bernoulli(close at t+H > recent high at t).
- ``mean_reversion_success`` → Bernoulli(close at t+H within band of mean).
- ``realised_vol_forward`` → realised vol of returns in (t, t+H].
- ``drawdown_probability`` → Bernoulli(min path return in (t, t+H] < -threshold).

Each returns a ``pd.Series`` aligned to ``close.index``.
"""

from __future__ import annotations

import math
from typing import Iterable

import numpy as np
import pandas as pd


TARGET_KINDS: tuple[str, ...] = (
    "forward_return",
    "breakout_continuation",
    "mean_reversion_success",
    "realised_vol_forward",
    "drawdown_probability",
)


def _validate_close(close: pd.Series) -> pd.Series:
    s = close.astype(float)
    if not s.index.is_monotonic_increasing:
        raise ValueError("close must be sorted ascending by index")
    return s


# ── return targets ──────────────────────────────────────────────────────────


def forward_return(close: pd.Series, *, horizon: int) -> pd.Series:
    """
    Simple return over the next ``horizon`` bars.

    ``y_i = close[i+horizon] / close[i] - 1.0``

    Trailing ``horizon`` rows are NaN — caller drops them.
    """
    if horizon <= 0:
        raise ValueError("horizon must be positive")
    px = _validate_close(close)
    fut = px.shift(-horizon)
    return (fut / px - 1.0).astype(float)


# ── continuation / mean-reversion ───────────────────────────────────────────


def breakout_continuation(
    close: pd.Series,
    *,
    horizon: int,
    lookback: int = 20,
) -> pd.Series:
    """
    1 if ``close[i+horizon] > rolling_high(close[..i], lookback)``, else 0.

    Trailing rows where ``i + horizon`` exceeds the series are NaN.
    """
    if horizon <= 0 or lookback <= 0:
        raise ValueError("horizon and lookback must be positive")
    px = _validate_close(close)
    high = px.rolling(lookback, min_periods=lookback).max()
    fut = px.shift(-horizon)
    out = (fut > high).astype(float)
    out[fut.isna() | high.isna()] = np.nan
    return out


def mean_reversion_success(
    close: pd.Series,
    *,
    horizon: int,
    lookback: int = 20,
    band: float = 0.005,
) -> pd.Series:
    """
    1 if ``close[i+horizon]`` is within ``band`` (relative) of the
    rolling mean over the last ``lookback`` bars; else 0.

    Useful to train a "is this likely to mean-revert within H?" model.
    """
    if horizon <= 0 or lookback <= 0:
        raise ValueError("horizon and lookback must be positive")
    px = _validate_close(close)
    mean_ = px.rolling(lookback, min_periods=lookback).mean()
    fut = px.shift(-horizon)
    deviation = (fut - mean_).abs() / mean_.abs()
    out = (deviation <= band).astype(float)
    out[fut.isna() | mean_.isna()] = np.nan
    return out


# ── volatility / drawdown ───────────────────────────────────────────────────


def realised_vol_forward(
    close: pd.Series,
    *,
    horizon: int,
    annualise: bool = True,
    periods_per_year: int = 252,
) -> pd.Series:
    """Realised vol of log returns over the *next* ``horizon`` bars."""
    if horizon <= 1:
        raise ValueError("horizon must be at least 2 for realised vol")
    px = _validate_close(close)
    log_ret = np.log(px).diff()
    n = len(px)
    out = np.full(n, np.nan, dtype=float)
    annualiser = math.sqrt(periods_per_year) if annualise else 1.0
    for i in range(n):
        end = i + horizon + 1
        if end > n:
            break
        window = log_ret.iloc[i + 1 : end]
        if window.dropna().shape[0] < max(2, horizon // 2):
            continue
        out[i] = float(window.std(ddof=1)) * annualiser
    return pd.Series(out, index=px.index, dtype=float)


def drawdown_probability(
    close: pd.Series,
    *,
    horizon: int,
    threshold: float = 0.05,
) -> pd.Series:
    """
    1 if the worst forward path return in (t, t+horizon] is below
    ``-threshold`` (e.g. ``threshold=0.05`` → 5% drawdown).
    """
    if horizon <= 0:
        raise ValueError("horizon must be positive")
    if threshold <= 0:
        raise ValueError("threshold must be positive")
    px = _validate_close(close)
    n = len(px)
    out = np.full(n, np.nan, dtype=float)
    for i in range(n):
        end = i + horizon + 1
        if end > n:
            break
        p0 = float(px.iloc[i])
        if p0 <= 0:
            continue
        path = px.iloc[i + 1 : end].astype(float)
        if path.empty:
            continue
        worst = float((path / p0 - 1.0).min())
        out[i] = 1.0 if worst <= -threshold else 0.0
    return pd.Series(out, index=px.index, dtype=float)


# ── catch-all ───────────────────────────────────────────────────────────────


def build_target(
    close: pd.Series,
    *,
    kind: str,
    horizon: int,
    **kwargs,
) -> pd.Series:
    """Dispatch by ``kind``. Raises on unknown kinds."""
    k = (kind or "").strip().lower()
    if k == "forward_return":
        return forward_return(close, horizon=horizon)
    if k == "breakout_continuation":
        return breakout_continuation(close, horizon=horizon, **kwargs)
    if k == "mean_reversion_success":
        return mean_reversion_success(close, horizon=horizon, **kwargs)
    if k == "realised_vol_forward":
        return realised_vol_forward(close, horizon=horizon, **kwargs)
    if k == "drawdown_probability":
        return drawdown_probability(close, horizon=horizon, **kwargs)
    raise ValueError(f"unknown forecast target kind: {kind!r}")


def is_classification_target(kind: str) -> bool:
    return kind in {"breakout_continuation", "mean_reversion_success", "drawdown_probability"}


def is_regression_target(kind: str) -> bool:
    return kind in {"forward_return", "realised_vol_forward"}


def supported_kinds() -> Iterable[str]:
    return TARGET_KINDS
