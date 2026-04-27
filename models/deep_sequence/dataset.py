"""
models/deep_sequence/dataset.py
=================================
Wave 11 — leakage-safe sliding windows.

Given a feature matrix ``F`` of shape ``(n_obs, n_features)`` and a
target series ``y`` of length ``n_obs``, ``make_sequence_windows``
emits:

    X[t]  = F[t - window + 1 : t + 1]            # shape (window, n_features)
    y[t]  = y[t + horizon]                       # forward target

Trailing rows where ``t + horizon`` overruns the series are dropped.
This is the canonical sequence-model setup; every deep architecture
(TCN / TFT / RNN) consumes the same shape.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass
class SequenceDataset:
    X: np.ndarray            # shape (n_samples, window, n_features)
    y: np.ndarray            # shape (n_samples,)
    timestamps: pd.DatetimeIndex
    feature_names: tuple[str, ...]
    window: int
    horizon: int


def make_sequence_windows(
    *,
    feature_frame: pd.DataFrame,
    target: pd.Series,
    window: int,
    horizon: int,
    drop_na: bool = True,
) -> SequenceDataset:
    """
    Build a leakage-safe sequence dataset.

    Inputs:
      - ``feature_frame``: DataFrame with a datetime index.
      - ``target``: Series aligned to the same index; ``y[t]`` is the
        realised value to predict from features up to and including
        ``t``.
      - ``window``: number of trailing rows that form one ``X[t]``.
      - ``horizon``: how far ahead the target lives — ``y_out[t] = target[t + horizon]``.

    Properties guaranteed:
      - For row ``t`` in the output, ``X[t]`` uses *only* feature rows
        ``[t - window + 1 .. t]`` of ``feature_frame``.
      - ``target`` at row ``t + horizon`` is read from the original
        series; no future feature information is mixed in.
      - Trailing rows where ``t + horizon`` exceeds the index are dropped.
    """
    if window <= 0:
        raise ValueError("window must be positive")
    if horizon < 0:
        raise ValueError("horizon must be non-negative")
    if not feature_frame.index.equals(target.index):
        target = target.reindex(feature_frame.index)

    F = feature_frame.to_numpy(dtype=float)
    y = target.to_numpy(dtype=float)
    n_obs, n_feat = F.shape if F.ndim == 2 else (len(F), 1)
    if F.ndim == 1:
        F = F.reshape(-1, 1)

    last_t = n_obs - 1 - horizon
    first_t = window - 1
    if last_t < first_t:
        return SequenceDataset(
            X=np.empty((0, window, n_feat)),
            y=np.empty((0,)),
            timestamps=pd.DatetimeIndex([]),
            feature_names=tuple(feature_frame.columns),
            window=window,
            horizon=horizon,
        )

    samples_X: list[np.ndarray] = []
    samples_y: list[float] = []
    samples_ts: list = []
    for t in range(first_t, last_t + 1):
        win = F[t - window + 1 : t + 1]
        if drop_na and (np.isnan(win).any() or np.isnan(y[t + horizon])):
            continue
        samples_X.append(win)
        samples_y.append(float(y[t + horizon]))
        samples_ts.append(feature_frame.index[t])

    if not samples_X:
        return SequenceDataset(
            X=np.empty((0, window, n_feat)),
            y=np.empty((0,)),
            timestamps=pd.DatetimeIndex([]),
            feature_names=tuple(feature_frame.columns),
            window=window,
            horizon=horizon,
        )

    return SequenceDataset(
        X=np.stack(samples_X, axis=0),
        y=np.asarray(samples_y, dtype=float),
        timestamps=pd.DatetimeIndex(samples_ts),
        feature_names=tuple(feature_frame.columns),
        window=window,
        horizon=horizon,
    )
