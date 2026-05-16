"""
models/deep_sequence/evaluate.py
==================================
Wave 11 — comparison harness.

The Wave 11 governance rule lives here in code:

    A deep sequence model is **promote-eligible** only when, on the
    held-out OOS window:

      1. mse(deep) / mse(baseline) <= mse_ratio_threshold      AND
      2. hit_rate(deep) - hit_rate(baseline) >= hit_rate_margin AND
      3. cost-aware net P&L of deep > cost-aware net P&L of baseline
      4. cost-aware net P&L of deep > 0

    The P&L rules are non-negotiable: a deep model that wins on metrics but
    loses to costs after slippage is not added value and must not be
promoted.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

import numpy as np


@dataclass
class BaselineComparisonReport:
    n_oos: int
    mse_baseline: float
    mse_deep: float
    mse_ratio: float                       # mse_deep / mse_baseline
    hit_rate_baseline: float
    hit_rate_deep: float
    hit_rate_margin_required: float
    net_pnl_baseline: float                # cost-adjusted, sum of sign(pred) * y - cost
    net_pnl_deep: float
    deep_beats_baseline: bool
    failures: list[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)


def _hit_rate(y: np.ndarray, pred: np.ndarray) -> float:
    sign_y = np.sign(y)
    sign_p = np.sign(pred)
    mask = (sign_y != 0) & (sign_p != 0)
    if mask.sum() == 0:
        return float("nan")
    return float((sign_y[mask] == sign_p[mask]).mean())


def _net_pnl(y: np.ndarray, pred: np.ndarray, *, round_trip_cost_bps: float) -> float:
    cost = float(round_trip_cost_bps) / 10_000.0
    direction = np.sign(pred)
    pnl = direction * y
    return float(np.sum(pnl - cost * (np.abs(direction) > 0).astype(float)))


def compare_against_baseline(
    *,
    y_true: Iterable[float],
    deep_predictions: Iterable[float],
    baseline_predictions: Iterable[float],
    mse_ratio_threshold: float = 0.95,
    hit_rate_margin: float = 0.01,
    round_trip_cost_bps: float = 5.0,
) -> BaselineComparisonReport:
    y = np.asarray(list(y_true), dtype=float).ravel()
    p_deep = np.asarray(list(deep_predictions), dtype=float).ravel()
    p_base = np.asarray(list(baseline_predictions), dtype=float).ravel()

    if not (len(y) == len(p_deep) == len(p_base)):
        raise ValueError("y_true / deep / baseline lengths must match")
    if len(y) == 0:
        return BaselineComparisonReport(
            n_oos=0, mse_baseline=float("nan"), mse_deep=float("nan"),
            mse_ratio=float("nan"),
            hit_rate_baseline=float("nan"), hit_rate_deep=float("nan"),
            hit_rate_margin_required=hit_rate_margin,
            net_pnl_baseline=0.0, net_pnl_deep=0.0,
            deep_beats_baseline=False,
            failures=["empty_oos"],
        )

    mse_b = float(np.mean((p_base - y) ** 2))
    mse_d = float(np.mean((p_deep - y) ** 2))
    ratio = mse_d / mse_b if mse_b > 0 else float("inf")
    hr_b = _hit_rate(y, p_base)
    hr_d = _hit_rate(y, p_deep)
    pnl_b = _net_pnl(y, p_base, round_trip_cost_bps=round_trip_cost_bps)
    pnl_d = _net_pnl(y, p_deep, round_trip_cost_bps=round_trip_cost_bps)

    failures: list[str] = []
    if ratio > mse_ratio_threshold:
        failures.append(f"mse_ratio={ratio:.3f}>thr={mse_ratio_threshold}")
    if not np.isnan(hr_b) and not np.isnan(hr_d) and (hr_d - hr_b) < hit_rate_margin:
        failures.append(f"hit_rate_margin={hr_d - hr_b:+.3f}<thr={hit_rate_margin}")
    if pnl_d <= pnl_b:
        failures.append(f"net_pnl_deep={pnl_d:.6f}<=baseline={pnl_b:.6f}")
    if pnl_d <= 0.0:
        failures.append(f"net_pnl_deep={pnl_d:.6f}<=0")

    deep_beats = len(failures) == 0

    return BaselineComparisonReport(
        n_oos=int(len(y)),
        mse_baseline=mse_b,
        mse_deep=mse_d,
        mse_ratio=float(ratio),
        hit_rate_baseline=float(hr_b) if not np.isnan(hr_b) else float("nan"),
        hit_rate_deep=float(hr_d) if not np.isnan(hr_d) else float("nan"),
        hit_rate_margin_required=float(hit_rate_margin),
        net_pnl_baseline=float(pnl_b),
        net_pnl_deep=float(pnl_d),
        deep_beats_baseline=bool(deep_beats),
        failures=failures,
        metadata={"round_trip_cost_bps": float(round_trip_cost_bps)},
    )
