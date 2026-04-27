"""
data/factor_features.py
========================
Wave 3 — price-based cross-sectional factor inputs.

Pure functions over a per-symbol price ``pd.Series`` (close prices,
sorted ascending by datetime, UTC). Each function returns a single
float (the factor value as of the *last* observation in the series),
or ``None`` when there isn't enough data.

Leakage guarantee: every factor uses *only* observations up to and
including the last bar in the input. Callers are responsible for
trimming the series so the "as-of" timestamp is correct.
"""

from __future__ import annotations

import math
from typing import Optional

import numpy as np
import pandas as pd


# ── helpers ──────────────────────────────────────────────────────────────────


def _safe_returns(close: pd.Series) -> pd.Series:
    """Compute simple returns and drop the first NaN."""
    ret = close.pct_change()
    return ret.iloc[1:].astype(float)


def _last_n(s: pd.Series, n: int) -> pd.Series:
    return s.iloc[-n:] if len(s) >= n else s


# ── momentum family ──────────────────────────────────────────────────────────


def momentum_12_1(close: pd.Series, *, total_bars: int = 252, skip_bars: int = 21) -> Optional[float]:
    """
    Classic 12-1 momentum: cumulative return from t-12m to t-1m, skipping
    the most recent month (reversal-noise filter). Defaults assume daily
    bars (252 trading days, 21 day skip). Returns ``None`` if the series
    is too short.
    """
    if len(close) < total_bars + 1:
        return None
    px = close.astype(float)
    end = px.iloc[-skip_bars - 1] if skip_bars > 0 else px.iloc[-1]
    start = px.iloc[-(total_bars + 1)]
    if start <= 0 or not math.isfinite(start):
        return None
    return float(end / start - 1.0)


def momentum_6m(close: pd.Series, *, bars: int = 126) -> Optional[float]:
    """6-month cumulative return."""
    if len(close) < bars + 1:
        return None
    px = close.astype(float)
    start = px.iloc[-(bars + 1)]
    if start <= 0:
        return None
    return float(px.iloc[-1] / start - 1.0)


def reversal_1m(close: pd.Series, *, bars: int = 21) -> Optional[float]:
    """
    1-month reversal: short-horizon return. Cross-sectional alpha papers
    typically use the *negative* of this (winners-of-last-month
    underperform). We return the raw return; ``composite_factor_score``
    applies the sign.
    """
    if len(close) < bars + 1:
        return None
    px = close.astype(float)
    start = px.iloc[-(bars + 1)]
    if start <= 0:
        return None
    return float(px.iloc[-1] / start - 1.0)


# ── volatility family ────────────────────────────────────────────────────────


def realised_vol(close: pd.Series, *, bars: int = 63) -> Optional[float]:
    """Annualised realised volatility from log returns. ``bars`` window."""
    if len(close) < bars + 1:
        return None
    rets = np.log(close.astype(float)).diff().iloc[1:].dropna()
    if len(rets) < bars:
        return None
    sample = rets.iloc[-bars:]
    sd = float(sample.std(ddof=1))
    if not math.isfinite(sd):
        return None
    # Annualise assuming the input cadence matches the bars/year ratio.
    # Daily ⇒ √252, hourly ⇒ √(24*252). Caller picks ``bars`` accordingly;
    # we annualise for the typical daily case but keep a generic √(bars).
    return sd * math.sqrt(252)


def downside_vol(close: pd.Series, *, bars: int = 63) -> Optional[float]:
    """Realised volatility of *negative* returns only (Sortino denominator)."""
    if len(close) < bars + 1:
        return None
    rets = np.log(close.astype(float)).diff().iloc[1:].dropna()
    if len(rets) < bars:
        return None
    sample = rets.iloc[-bars:]
    neg = sample[sample < 0]
    if len(neg) < 2:
        return 0.0
    return float(neg.std(ddof=1)) * math.sqrt(252)


def drawdown_stability(close: pd.Series, *, bars: int = 252) -> Optional[float]:
    """
    A defensive-quality proxy: 1 - max drawdown over the window. Higher
    is better (smaller drawdown).
    """
    if len(close) < min(bars, 30):
        return None
    px = _last_n(close.astype(float), bars)
    cummax = px.cummax()
    dd = (px / cummax - 1.0).min()
    if not math.isfinite(dd):
        return None
    return float(1.0 + dd)  # dd is negative ⇒ result <= 1


# ── beta / co-movement ───────────────────────────────────────────────────────


def beta_to_benchmark(
    close: pd.Series,
    benchmark_close: pd.Series,
    *,
    bars: int = 252,
) -> Optional[float]:
    """OLS beta of asset returns on benchmark returns over ``bars`` window."""
    if len(close) < bars + 1 or len(benchmark_close) < bars + 1:
        return None
    a = _safe_returns(close).iloc[-bars:]
    b = _safe_returns(benchmark_close).iloc[-bars:]
    df = pd.concat([a, b], axis=1, join="inner").dropna()
    if len(df) < bars // 2:
        return None
    cov = float(df.cov().iloc[0, 1])
    var = float(df.iloc[:, 1].var(ddof=1))
    if var <= 0 or not math.isfinite(var):
        return None
    return cov / var


# ── catch-all builder ────────────────────────────────────────────────────────


def build_price_factors(
    close: pd.Series,
    *,
    benchmark_close: Optional[pd.Series] = None,
    daily: bool = True,
) -> dict[str, Optional[float]]:
    """
    Compute the standard price-based factor block for one symbol.

    ``daily=True`` uses default windows tuned for daily bars. For other
    cadences (hourly, weekly), pass series with the matching frequency
    and the windows still apply to *bar count* — annualisation in
    ``realised_vol`` assumes daily.
    """
    out: dict[str, Optional[float]] = {
        "momentum_12_1": momentum_12_1(close),
        "momentum_6m": momentum_6m(close),
        "reversal_1m": reversal_1m(close),
        "realised_vol": realised_vol(close),
        "downside_vol": downside_vol(close),
        "drawdown_stability": drawdown_stability(close),
    }
    if benchmark_close is not None:
        out["beta"] = beta_to_benchmark(close, benchmark_close)
    return out
