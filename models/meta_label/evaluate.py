"""
models/meta_label/evaluate.py
==============================
Wave 2 — standalone evaluation utilities.

The training routine in ``train.py`` already produces a
``MetaLabelEvalReport`` with purged-CV metrics. This module adds
post-hoc evaluators that are useful in research scripts but should
not run in the trading loop:

- ``evaluate_calibration`` — reliability-curve summary.
- ``evaluate_per_regime`` — metric breakdown by an arbitrary group
  column (e.g. regime label, asset class, strategy).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

import numpy as np
import pandas as pd


@dataclass
class CalibrationSummary:
    n_bins: int
    bin_centers: list[float]
    bin_means_predicted: list[float]
    bin_means_observed: list[float]
    bin_counts: list[int]
    expected_calibration_error: float


def evaluate_calibration(
    y_true: Iterable[int],
    p_pred: Iterable[float],
    *,
    n_bins: int = 10,
) -> CalibrationSummary:
    yt = np.asarray(list(y_true), dtype=float)
    pp = np.asarray(list(p_pred), dtype=float).clip(0.0, 1.0)
    edges = np.linspace(0.0, 1.0, n_bins + 1)
    centers: list[float] = []
    means_pred: list[float] = []
    means_obs: list[float] = []
    counts: list[int] = []
    ece = 0.0
    n = len(pp)
    for i in range(n_bins):
        lo, hi = edges[i], edges[i + 1]
        mask = (pp >= lo) & ((pp < hi) if i < n_bins - 1 else (pp <= hi))
        c = int(mask.sum())
        if c == 0:
            continue
        mp = float(pp[mask].mean())
        mo = float(yt[mask].mean())
        centers.append(0.5 * (lo + hi))
        means_pred.append(mp)
        means_obs.append(mo)
        counts.append(c)
        ece += (c / max(1, n)) * abs(mp - mo)
    return CalibrationSummary(
        n_bins=n_bins,
        bin_centers=centers,
        bin_means_predicted=means_pred,
        bin_means_observed=means_obs,
        bin_counts=counts,
        expected_calibration_error=float(ece),
    )


def evaluate_per_regime(
    *,
    y_true: pd.Series,
    p_pred: pd.Series,
    group: pd.Series,
    threshold: float = 0.55,
) -> pd.DataFrame:
    """Per-group hit rate, base rate, and Brier."""
    df = pd.DataFrame(
        {"y": y_true.values, "p": p_pred.values, "g": group.values}
    ).dropna()
    out = []
    for g, sub in df.groupby("g"):
        take = sub["p"] >= threshold
        hit = float(sub.loc[take, "y"].mean()) if take.any() else float("nan")
        out.append(
            {
                "group": g,
                "n": int(len(sub)),
                "n_taken": int(take.sum()),
                "base_rate": float(sub["y"].mean()),
                "hit_rate@thr": hit,
                "brier": float(((sub["p"] - sub["y"]) ** 2).mean()),
            }
        )
    return pd.DataFrame(out)
