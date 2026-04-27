"""
models/pairs/
==============
Wave 5 — research-grade relative-value modelling for pairs trading.

Public surface:

- ``compute_spread``, ``spread_zscore``, ``half_life_ou`` — spread maths.
- ``engle_granger_test``, ``johansen_eigen_test`` — cointegration screens.
- ``KalmanHedgeRatio`` — online time-varying hedge ratio.
- ``discover_pair_candidates`` — universe-level pair discovery.
- ``detect_spread_break``, ``detect_correlation_decay``,
  ``transaction_cost_aware_thresholds`` — pair risk monitors.

The runtime call site lives in ``strategies/stat_arb_pairs.py``.
"""

from models.pairs.johansen import (
    EngleGrangerResult,
    JohansenResult,
    engle_granger_test,
    johansen_eigen_test,
)
from models.pairs.kalman import KalmanHedgeRatio, KalmanState
from models.pairs.risk import (
    SpreadBreakResult,
    detect_correlation_decay,
    detect_spread_break,
    transaction_cost_aware_thresholds,
)
from models.pairs.spread import (
    compute_spread,
    half_life_ou,
    spread_zscore,
)
from models.pairs.universe import (
    PairCandidate,
    discover_pair_candidates,
)

__all__ = [
    "EngleGrangerResult",
    "JohansenResult",
    "KalmanHedgeRatio",
    "KalmanState",
    "PairCandidate",
    "SpreadBreakResult",
    "compute_spread",
    "detect_correlation_decay",
    "detect_spread_break",
    "discover_pair_candidates",
    "engle_granger_test",
    "half_life_ou",
    "johansen_eigen_test",
    "spread_zscore",
    "transaction_cost_aware_thresholds",
]
