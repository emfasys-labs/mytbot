"""
models/pairs/universe.py
==========================
Wave 5 — pair candidate discovery.

Given a universe of price series, rank candidate pairs by:

  1. Trailing log-return correlation (filter out the obvious garbage).
  2. Engle-Granger ADF screen on the residuals (the cointegration
     gate).
  3. Estimated OU half-life (sanity check — a "cointegrated" pair
     whose half-life is months long is useless).

Returns a list of ``PairCandidate`` sorted by a composite score.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import combinations
from typing import Iterable, Mapping, Optional

import numpy as np
import pandas as pd

from models.pairs.johansen import EngleGrangerResult, engle_granger_test
from models.pairs.spread import compute_spread, half_life_ou


@dataclass
class PairCandidate:
    leg_a: str
    leg_b: str
    correlation: float
    eg_result: EngleGrangerResult
    half_life_bars: Optional[float]
    composite_score: float = 0.0
    notes: str = ""


def _composite_score(corr: float, p: float, hl: Optional[float]) -> float:
    """
    Higher is better. Components:

      - correlation magnitude (0..1) → linear contribution.
      - 1 - p_value (so cointegrated pairs get a boost) → linear.
      - half-life: ideal regime ~ [5, 60] bars; outside that range
        we tail off linearly.
    """
    abs_corr = float(abs(corr))
    p_score = max(0.0, 1.0 - float(p))
    if hl is None or hl <= 0:
        hl_score = 0.0
    elif 5 <= hl <= 60:
        hl_score = 1.0
    elif hl < 5:
        hl_score = max(0.0, hl / 5.0)
    else:
        hl_score = max(0.0, 1.0 - (hl - 60) / 200.0)
    return 0.3 * abs_corr + 0.5 * p_score + 0.2 * hl_score


def discover_pair_candidates(
    prices: Mapping[str, pd.Series],
    *,
    min_correlation: float = 0.6,
    max_p_value: float = 0.05,
    max_half_life_bars: float = 200.0,
    top_n: int = 20,
    symbols_to_screen: Optional[Iterable[str]] = None,
) -> list[PairCandidate]:
    """
    Iterate over every (leg_a, leg_b) combination. Filter by
    ``min_correlation`` first — that's the cheap gate — then run the
    Engle-Granger test, then half-life. Sort by composite score.
    """
    syms = list(symbols_to_screen) if symbols_to_screen is not None else list(prices.keys())
    if len(syms) < 2:
        return []

    # Pre-compute log returns for the cheap correlation pass.
    log_rets: dict[str, pd.Series] = {}
    for s in syms:
        if s not in prices:
            continue
        log_rets[s] = np.log(prices[s].astype(float)).diff().dropna()

    out: list[PairCandidate] = []
    for a, b in combinations(syms, 2):
        if a not in log_rets or b not in log_rets:
            continue
        df = pd.concat([log_rets[a], log_rets[b]], axis=1, join="inner").dropna()
        if len(df) < 60:
            continue
        corr = float(df.iloc[:, 0].corr(df.iloc[:, 1]))
        if abs(corr) < min_correlation:
            continue
        eg = engle_granger_test(prices[a], prices[b])
        if not np.isfinite(eg.adf_stat) or eg.p_value_estimate > max_p_value:
            continue
        spread = compute_spread(prices[a], prices[b], beta=eg.beta, intercept=eg.intercept)
        hl = half_life_ou(spread)
        if hl is None or hl <= 0 or hl > max_half_life_bars:
            continue
        score = _composite_score(corr, eg.p_value_estimate, hl)
        out.append(
            PairCandidate(
                leg_a=a,
                leg_b=b,
                correlation=corr,
                eg_result=eg,
                half_life_bars=hl,
                composite_score=score,
            )
        )

    out.sort(key=lambda c: c.composite_score, reverse=True)
    return out[: int(max(0, top_n))]
