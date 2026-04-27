"""
models/forecasts/evaluate.py
=============================
Wave 6 — evaluation helpers for tabular forecasts.

Used by ``scripts/evaluate_forecasts.py`` and any research notebook;
not invoked from the trading loop.

Functions:

- ``compute_information_coefficient`` — Spearman IC between forecast
  and realised target.
- ``compute_hit_rate_after_costs`` — fraction of trades where
  ``sign(forecast) * realised > round_trip_cost``.
- ``compute_calibration_summary`` — reliability curve for classification
  forecasts.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

import numpy as np
import pandas as pd


def compute_information_coefficient(
    y_true: Iterable[float],
    y_pred: Iterable[float],
) -> float:
    yt = pd.Series(list(y_true)).astype(float)
    yp = pd.Series(list(y_pred)).astype(float)
    df = pd.DataFrame({"yt": yt, "yp": yp}).dropna()
    if len(df) < 3:
        return float("nan")
    a = df["yt"].rank(pct=True).to_numpy()
    b = df["yp"].rank(pct=True).to_numpy()
    if np.std(a) == 0 or np.std(b) == 0:
        return 0.0
    return float(np.corrcoef(a, b)[0, 1])


def compute_hit_rate_after_costs(
    y_true: Iterable[float],
    y_pred: Iterable[float],
    *,
    round_trip_cost: float = 0.0005,
) -> float:
    """
    Fraction of forecasted trades whose realised return exceeds the
    round-trip cost (sign-adjusted by forecast direction).
    """
    yt = np.asarray(list(y_true), dtype=float)
    yp = np.asarray(list(y_pred), dtype=float)
    if len(yt) == 0:
        return float("nan")
    direction = np.sign(yp)
    pnl = direction * yt
    return float((pnl > round_trip_cost).mean())


@dataclass
class CalibrationSummary:
    n_bins: int
    bin_centers: list[float]
    bin_means_predicted: list[float]
    bin_means_observed: list[float]
    bin_counts: list[int]
    expected_calibration_error: float


def compute_calibration_summary(
    y_true: Iterable[int],
    p_pred: Iterable[float],
    *,
    n_bins: int = 10,
) -> CalibrationSummary:
    yt = np.asarray(list(y_true), dtype=float)
    pp = np.clip(np.asarray(list(p_pred), dtype=float), 0.0, 1.0)
    edges = np.linspace(0.0, 1.0, n_bins + 1)
    centers, mp, mo, counts = [], [], [], []
    ece = 0.0
    n = len(pp)
    for i in range(n_bins):
        lo, hi = edges[i], edges[i + 1]
        mask = (pp >= lo) & ((pp < hi) if i < n_bins - 1 else (pp <= hi))
        c = int(mask.sum())
        if c == 0:
            continue
        centers.append(0.5 * (lo + hi))
        mp.append(float(pp[mask].mean()))
        mo.append(float(yt[mask].mean()))
        counts.append(c)
        ece += (c / max(1, n)) * abs(mp[-1] - mo[-1])
    return CalibrationSummary(
        n_bins=n_bins,
        bin_centers=centers,
        bin_means_predicted=mp,
        bin_means_observed=mo,
        bin_counts=counts,
        expected_calibration_error=float(ece),
    )
