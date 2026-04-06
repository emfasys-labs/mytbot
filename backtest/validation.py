"""
Backtest validation helpers for M3:
- Purged time-series splits (embargo)
- Deflated Sharpe approximation
- PBO-style path metric
"""

from __future__ import annotations

import math
from statistics import NormalDist

import numpy as np
import pandas as pd

try:  # Optional strict research dependency.
    from timeseriescv.cross_validation import CombPurgedKFoldCV  # type: ignore
except Exception:  # noqa: BLE001
    CombPurgedKFoldCV = None


def purged_time_series_splits(
    n_samples: int,
    *,
    n_splits: int,
    embargo_bars: int,
) -> list[tuple[tuple[int, int], tuple[int, int]]]:
    """
    Return splits as ((train_start, train_end), (test_start, test_end)).
    Half-open intervals; train and test are non-overlapping with embargo gap.
    """
    if n_samples <= 0 or n_splits <= 1:
        return []
    fold = max(1, n_samples // n_splits)
    out: list[tuple[tuple[int, int], tuple[int, int]]] = []
    for i in range(n_splits):
        test_start = i * fold
        test_end = n_samples if i == n_splits - 1 else min(n_samples, (i + 1) * fold)
        if test_start >= test_end:
            continue
        left_end = max(0, test_start - embargo_bars)
        right_start = min(n_samples, test_end + embargo_bars)
        # Use pre-test train slice; if too small, use post-test.
        if left_end >= fold:
            train = (0, left_end)
        elif right_start < n_samples - 1:
            train = (right_start, n_samples)
        else:
            continue
        out.append((train, (test_start, test_end)))
    return out


def combinatorial_purged_splits(
    n_samples: int,
    *,
    n_splits: int,
    n_test_splits: int,
    embargo_bars: int,
) -> list[tuple[list[int], list[int]]]:
    """
    Strict mode: use timeseriescv.CombPurgedKFoldCV when installed.
    Returns explicit (train_indices, test_indices) folds.
    Falls back to simple purged splits if dependency is unavailable.
    """
    if n_samples <= 0:
        return []
    if CombPurgedKFoldCV is None:
        base = purged_time_series_splits(
            n_samples, n_splits=max(2, n_splits), embargo_bars=max(0, embargo_bars)
        )
        out: list[tuple[list[int], list[int]]] = []
        for train, test in base:
            tr0, tr1 = train
            te0, te1 = test
            out.append((list(range(tr0, tr1)), list(range(te0, te1))))
        return out

    X = np.arange(n_samples)
    cv = CombPurgedKFoldCV(
        n_splits=max(2, n_splits),
        n_test_splits=max(1, n_test_splits),
        embargo_td=pd.Timedelta(days=max(0, embargo_bars)),
    )
    idx = pd.date_range("2000-01-01", periods=n_samples, freq="D")
    x = pd.DataFrame({"x": X}, index=idx)
    pred_times = pd.Series(idx, index=idx)
    eval_times = pred_times + pd.Timedelta(days=1)
    folds: list[tuple[list[int], list[int]]] = []
    for train_idx, test_idx in cv.split(
        x,
        pred_times=pred_times,
        eval_times=eval_times,
    ):
        folds.append((list(train_idx), list(test_idx)))
    return folds


def annualized_sharpe_from_returns(returns: list[float], periods_per_year: int = 252) -> float:
    if not returns:
        return 0.0
    n = len(returns)
    mean = sum(returns) / n
    var = sum((x - mean) ** 2 for x in returns) / max(1, n - 1)
    std = math.sqrt(var)
    if std <= 0:
        return 0.0
    return (mean / std) * math.sqrt(periods_per_year)


def deflated_sharpe_ratio(
    sharpe: float,
    *,
    n_trials: int,
    n_obs: int,
    skew: float = 0.0,
    kurtosis: float = 3.0,
) -> float:
    """
    Approximate DSR in [0,1].
    """
    if n_obs <= 1:
        return 0.0
    n_trials = max(1, n_trials)
    # Expected max Sharpe benchmark under multiple testing.
    sr0 = math.sqrt(2.0 * math.log(n_trials))
    denom = math.sqrt(max(1e-12, 1.0 - skew * sharpe + ((kurtosis - 1.0) / 4.0) * (sharpe**2)))
    z = (sharpe - sr0) * math.sqrt(n_obs - 1) / denom
    return float(NormalDist().cdf(z))


def pbo_from_path_scores(path_scores: list[float]) -> float:
    """
    Simple PBO-style estimate: fraction of path scores <= 0.
    """
    if not path_scores:
        return 1.0
    bad = sum(1 for s in path_scores if s <= 0.0)
    return bad / len(path_scores)

