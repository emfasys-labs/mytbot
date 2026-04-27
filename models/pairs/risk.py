"""
models/pairs/risk.py
======================
Wave 5 — pair-level risk monitors.

Three responsibilities:

- ``detect_spread_break``: when the spread z-score blows past a hard
  threshold AND the recent half-life has exploded (or estimation
  returns ``None``), declare the pair *broken* and flag it for
  liquidation.
- ``detect_correlation_decay``: rolling correlation between the two
  legs has dropped below a floor — the relative-value thesis is dying
  even if the spread hasn't broken yet.
- ``transaction_cost_aware_thresholds``: given an estimated round-trip
  cost in bps, set entry/exit z-thresholds that ensure the gross edge
  exceeds the cost.

All pure functions; the strategy module composes them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd

from models.pairs.spread import half_life_ou


@dataclass
class SpreadBreakResult:
    is_broken: bool
    reason: str
    latest_zscore: Optional[float] = None
    half_life_bars: Optional[float] = None


def detect_spread_break(
    spread_z: pd.Series,
    raw_spread: pd.Series,
    *,
    z_threshold: float = 4.0,
    half_life_ceiling_bars: float = 200.0,
    lookback: int = 60,
) -> SpreadBreakResult:
    """
    Declare the spread broken when the z-score has gone beyond
    ``±z_threshold`` *and* the trailing OU half-life is missing or
    exceeds ``half_life_ceiling_bars``.

    ``z_threshold`` alone isn't sufficient: a single fat-tail bar can
    spike the z-score in an otherwise healthy pair. The half-life
    check catches the genuine "the relationship has dissolved" case.
    """
    zs = spread_z.dropna()
    if len(zs) == 0:
        return SpreadBreakResult(is_broken=False, reason="no_data")
    latest = float(zs.iloc[-1])
    if abs(latest) < z_threshold:
        return SpreadBreakResult(
            is_broken=False, reason="zscore_within_band", latest_zscore=latest
        )
    # Spread is at the tail; check half-life over the lookback window.
    window = raw_spread.dropna().iloc[-int(lookback):]
    hl = half_life_ou(window)
    if hl is None or hl > half_life_ceiling_bars:
        return SpreadBreakResult(
            is_broken=True,
            reason="extreme_zscore_and_no_mean_reversion",
            latest_zscore=latest,
            half_life_bars=hl,
        )
    return SpreadBreakResult(
        is_broken=False,
        reason="extreme_zscore_but_still_mean_reverting",
        latest_zscore=latest,
        half_life_bars=hl,
    )


def detect_correlation_decay(
    y: pd.Series,
    x: pd.Series,
    *,
    window: int = 60,
    floor: float = 0.5,
) -> tuple[bool, Optional[float]]:
    """
    Rolling Pearson correlation between log returns of the two legs.
    Returns ``(decayed, latest_corr)``. ``decayed=True`` ⇒ correlation
    has fallen below ``floor``.
    """
    ry = np.log(y.astype(float)).diff()
    rx = np.log(x.astype(float)).diff()
    df = pd.concat([ry, rx], axis=1, join="inner").dropna()
    if len(df) < window:
        return False, None
    c = df.iloc[:, 0].rolling(window).corr(df.iloc[:, 1])
    latest = c.iloc[-1] if not c.empty else float("nan")
    if pd.isna(latest):
        return False, None
    return bool(float(latest) < floor), float(latest)


def transaction_cost_aware_thresholds(
    *,
    spread_sigma: float,
    round_trip_cost_bps: float,
    min_entry_z: float = 1.5,
    safety_multiplier: float = 1.2,
) -> tuple[float, float]:
    """
    Convert a round-trip cost (bps) into z-score entry / exit
    thresholds, given the typical spread standard deviation.

    A naive entry z of 2.0 may not cover the round-trip cost when the
    spread itself is small. We require:

        entry_z * spread_sigma * safety_multiplier  >=  round_trip_cost / 10_000

    Returns ``(entry_z, exit_z)`` with ``exit_z = 0`` (mean revert to
    equilibrium); the operator can override.
    """
    if spread_sigma <= 0:
        return float(min_entry_z), 0.0
    cost = max(0.0, float(round_trip_cost_bps)) / 10_000.0
    needed = float(safety_multiplier) * cost / float(spread_sigma)
    entry_z = max(float(min_entry_z), needed)
    return float(entry_z), 0.0
