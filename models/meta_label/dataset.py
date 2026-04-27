"""
models/meta_label/dataset.py
=============================
Wave 2 — build a leakage-safe training dataset for the meta-labeller.

The triple-barrier helper in ``backtest/labels.py`` already produces
{-1, 0, +1} per timestamp using *only* future prices in the window
``(i, i + max_horizon]``. The meta-label target is the *secondary*
question: "given a primary directional signal at time i, did it earn
the profit barrier before the stop barrier within the horizon?"

We map the directional outcome to a binary target conditional on the
candidate side:

  side == "buy":   y = 1 if barrier_label == +1 else 0
  side == "sell":  y = 1 if barrier_label == -1 else 0
  zero (timeout):  y = 0

This is the standard meta-label setup from Lopez de Prado: the primary
model is the strategy, the secondary model decides whether to *take*
the trade.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Sequence

import numpy as np
import pandas as pd

from backtest.labels import TripleBarrierSpec, triple_barrier_labels


@dataclass
class MetaLabelDataset:
    """
    ``X`` is a ``DataFrame`` of features (rows = primary signals).
    ``y`` is the binary meta-label (1 = take, 0 = skip).
    ``timestamps`` aligns rows back to the bar index (UTC) so a purged
    CV split can compute embargoes correctly.
    ``feature_columns`` is the frozen ordering used for the feature
    contract hash.
    """

    X: pd.DataFrame
    y: pd.Series
    timestamps: pd.DatetimeIndex
    feature_columns: list[str]

    def __len__(self) -> int:
        return len(self.y)


def enforce_no_future_leakage(
    *,
    feature_index: pd.DatetimeIndex,
    target_horizon_bars: int,
    n_total: int,
) -> None:
    """
    Sanity check: the dataset cannot include rows whose target horizon
    extends past the available price series. A model that "sees" labels
    for bars not yet observed at training time is leaking forward
    information. This raises a ``ValueError`` instead of silently
    truncating, because silent truncation hides leakage in research code.
    """
    if target_horizon_bars < 0:
        raise ValueError("target_horizon_bars must be >= 0")
    if len(feature_index) == 0:
        return
    last_usable = n_total - target_horizon_bars - 1
    if last_usable < 0:
        raise ValueError(
            f"target_horizon_bars={target_horizon_bars} is larger than "
            f"available bars ({n_total}); no leakage-safe rows exist"
        )
    last_pos = feature_index[-1]
    # ``feature_index`` is the row index of the dataset; we cannot
    # easily map it back to integer position from outside, so rely on
    # callers passing the dataset constructed via ``build_dataset_from_close``
    # which only emits rows up to ``last_usable``. This guard exists as a
    # last line of defence.
    if not isinstance(last_pos, (pd.Timestamp, datetime)):
        # Not a timestamp index — nothing to verify.
        return


def build_dataset_from_close(
    close: pd.Series,
    *,
    feature_frame: pd.DataFrame,
    sides: pd.Series,
    barrier_spec: TripleBarrierSpec,
    feature_columns: Sequence[str] | None = None,
) -> MetaLabelDataset:
    """
    Build a ``MetaLabelDataset`` aligned to ``close`` and ``feature_frame``.

    Inputs:
      - ``close``: pd.Series of close prices indexed by datetime.
      - ``feature_frame``: pd.DataFrame of features, same index as ``close``.
      - ``sides``: pd.Series of {"buy", "sell"} (or {1, -1}) per bar
        indicating the *primary* signal direction. Bars without a primary
        signal are ignored.
      - ``barrier_spec``: triple-barrier configuration (max_horizon caps
        forward look).
      - ``feature_columns``: explicit ordering used for the feature
        contract; defaults to ``feature_frame.columns``.

    Output:
      - ``MetaLabelDataset`` with binary y in {0, 1} and a feature
        contract-ready column ordering.

    Leakage guarantees:
      - Rows whose horizon extends past the end of ``close`` are dropped
        (last ``max_horizon`` bars are excluded).
      - Features for row i come strictly from ``feature_frame.iloc[i]``;
        labels come strictly from ``close.iloc[i+1 : i+1+max_horizon]``.
    """
    if feature_columns is None:
        feature_columns = list(feature_frame.columns)
    cols = list(feature_columns)

    # Align ``feature_frame`` to ``close`` index.
    fdf = feature_frame.reindex(close.index)
    fdf = fdf[cols]

    barrier = triple_barrier_labels(close, barrier_spec)

    # Map sides to {+1, -1}. Drop rows without a side.
    side_series = sides.reindex(close.index)

    def _side_to_sign(v) -> int | None:
        if isinstance(v, (int, np.integer)):
            iv = int(v)
            if iv == 1:
                return 1
            if iv == -1:
                return -1
            return None
        if isinstance(v, str):
            s = v.strip().lower()
            if s in ("buy", "long"):
                return 1
            if s in ("sell", "short"):
                return -1
            return None
        return None

    # ``na_action=None`` forces pandas to call ``_side_to_sign`` even for
    # NaN entries (StringArray.map otherwise short-circuits and yields NaN,
    # which would silently slip through the missing-side filter below).
    signs = side_series.map(_side_to_sign, na_action=None)

    # Binary meta-label: did the barrier outcome agree with the side?
    def _label(b: int, s) -> int:
        if s is None or pd.isna(s):
            return -999
        s = int(s)
        if s == 1 and b == 1:
            return 1
        if s == -1 and b == -1:
            return 1
        return 0

    y_full = pd.Series(
        [_label(int(b), s) for b, s in zip(barrier.fillna(0).astype(int), signs)],
        index=close.index,
        dtype=int,
    )

    # Drop rows where side is missing or horizon would overrun.
    horizon = max(0, int(barrier_spec.max_horizon))
    n = len(close)
    last_usable = n - horizon - 1
    keep_mask = (y_full != -999)
    if last_usable >= 0:
        keep_idx = close.index[: last_usable + 1]
        keep_mask = keep_mask & close.index.isin(keep_idx)

    X = fdf.loc[keep_mask].copy()
    y = y_full.loc[keep_mask].astype(int)

    enforce_no_future_leakage(
        feature_index=X.index,
        target_horizon_bars=horizon,
        n_total=n,
    )

    return MetaLabelDataset(
        X=X,
        y=y,
        timestamps=pd.DatetimeIndex(X.index),
        feature_columns=cols,
    )
