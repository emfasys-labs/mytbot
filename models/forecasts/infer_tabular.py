"""
models/forecasts/infer_tabular.py
===================================
Wave 6 — runtime inference helpers for tabular forecasts.

Tiny by design — the heavy lifting (loading via registry, fallback
matrix, dashboard metadata) lives in ``signals/forecast_bridge.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd

from models.forecasts.train_tabular import TrainedForecastModel


@dataclass
class ForecastResult:
    """One forecast row, ready for the dashboard funnel."""

    target_kind: str
    horizon: int
    value: Optional[float]  # regression: predicted y; classification: probability
    is_classification: bool
    model_name: Optional[str] = None
    model_version: Optional[str] = None
    feature_hash: Optional[str] = None
    metadata: dict[str, object] = field(default_factory=dict)


def score_forecast(
    artefact: TrainedForecastModel,
    X: pd.DataFrame | np.ndarray,
) -> np.ndarray:
    """Return predictions per row (calibrated probability for classification)."""
    return np.asarray(artefact.predict(X), dtype=float).ravel()
