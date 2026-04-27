"""
models/microstructure/
========================
Wave 10 — short-horizon LOB-driven forecasting.

Public surface:

- ``stack_lob_features`` — turn a list of ``OrderbookSnapshot`` + future
  return labels into a leakage-safe (X, y, timestamps) tuple.
- ``train_imbalance_forecaster`` — logistic baseline relating the
  feature block to the sign of the next short-horizon return.
- ``score_orderbook(snapshot, artefact, *, max_staleness_seconds)`` —
  freshness-gated runtime helper. Returns ``None`` when the book is
  stale or malformed.
- ``LOBForecastResult`` — runtime decision struct for the dashboard.

Boundary discipline: this package does not import ``brokers.*`` and
does not place orders. The execution scheduler (Wave 9) reads
``LOBForecastResult.imbalance_signal`` to nudge urgency choices in a
follow-up wiring step.
"""

from models.microstructure.features import (
    LOBFeatureSet,
    stack_lob_features,
)
from models.microstructure.imbalance import (
    LOBForecastResult,
    score_orderbook,
)
from models.microstructure.train_lob import (
    LOBEvalReport,
    TrainedLOBForecaster,
    train_imbalance_forecaster,
)

__all__ = [
    "LOBEvalReport",
    "LOBFeatureSet",
    "LOBForecastResult",
    "TrainedLOBForecaster",
    "score_orderbook",
    "stack_lob_features",
    "train_imbalance_forecaster",
]
