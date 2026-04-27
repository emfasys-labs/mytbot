"""
models/forecasts/dataset.py
=============================
Wave 6 — leakage-safe training dataset for forecasting.

Operates on a per-symbol close-price series + a pre-computed feature
DataFrame. Drops the trailing ``horizon`` rows whose target would
otherwise reach past the end of the series.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

import pandas as pd

from models.forecasts.targets import build_target, is_classification_target


@dataclass
class ForecastDataset:
    X: pd.DataFrame
    y: pd.Series
    timestamps: pd.DatetimeIndex
    feature_columns: list[str]
    target_kind: str
    horizon: int
    is_classification: bool

    def __len__(self) -> int:
        return len(self.y)


def build_forecast_dataset_from_close(
    close: pd.Series,
    *,
    feature_frame: pd.DataFrame,
    target_kind: str,
    horizon: int,
    feature_columns: Sequence[str] | None = None,
    target_kwargs: dict | None = None,
) -> ForecastDataset:
    """
    Build a forecast training dataset.

    Returns a ``ForecastDataset`` whose ``X`` and ``y`` are aligned
    on the feature index. Trailing rows where the target window
    overruns the series are dropped (no leakage).
    """
    if not close.index.is_monotonic_increasing:
        raise ValueError("close must be sorted ascending by index")
    if not feature_frame.index.equals(close.index):
        feature_frame = feature_frame.reindex(close.index)
    if feature_columns is None:
        feature_columns = list(feature_frame.columns)
    cols = list(feature_columns)
    fdf = feature_frame[cols]

    target = build_target(close, kind=target_kind, horizon=horizon, **(target_kwargs or {}))
    keep_mask = target.notna() & fdf.notna().all(axis=1)
    X = fdf.loc[keep_mask].copy()
    y = target.loc[keep_mask].astype(float)

    return ForecastDataset(
        X=X,
        y=y,
        timestamps=pd.DatetimeIndex(X.index),
        feature_columns=cols,
        target_kind=target_kind,
        horizon=int(horizon),
        is_classification=is_classification_target(target_kind),
    )
