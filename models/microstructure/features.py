"""
models/microstructure/features.py
===================================
Wave 10 — feature stacker for LOB training.

Turns a sequence of ``OrderbookSnapshot`` plus future-return labels
into a leakage-safe ``(X, y, timestamps)`` tuple. Features are exactly
the keys returned by ``data.orderbook_features.build_orderbook_features``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

import numpy as np
import pandas as pd

from data.orderbook_features import (
    OrderbookSnapshot,
    build_orderbook_features,
)


FEATURE_NAMES: tuple[str, ...] = (
    "spread_bps",
    "top_of_book_imbalance",
    "depth_imbalance",
    "book_slope",
    "liquidity_fragility",
    "vpin_proxy",
)
# ``quote_staleness`` is intentionally NOT a model input — it varies by
# wall-clock and would dominate prediction post-deployment. The freshness
# gate in ``models/microstructure/imbalance.score_orderbook`` consumes it
# directly.


@dataclass
class LOBFeatureSet:
    X: pd.DataFrame
    y: pd.Series
    timestamps: pd.DatetimeIndex
    feature_names: tuple[str, ...]


def stack_lob_features(
    snapshots: Sequence[OrderbookSnapshot],
    forward_returns: Iterable[float],
    *,
    depth: int = 5,
    classification: bool = True,
) -> LOBFeatureSet:
    """
    ``forward_returns[i]`` is the realised forward return *after* the
    ``snapshots[i]`` timestamp. Use ``NaN`` for trailing rows whose
    horizon would overrun the price series — they are dropped here.

    ``classification=True`` ⇒ ``y = (forward_return > 0).astype(int)``.
    """
    rows = []
    ts = []
    rets = list(forward_returns)
    if len(rets) != len(snapshots):
        raise ValueError("forward_returns length must match snapshots length")

    for snap, fr in zip(snapshots, rets):
        if fr is None or (isinstance(fr, float) and np.isnan(fr)):
            continue
        feats = build_orderbook_features(snap, depth=depth)
        if feats.get("well_formed", 0.0) <= 0:
            continue
        if any(feats.get(k) is None for k in FEATURE_NAMES if k != "quote_staleness"):
            continue
        row = [float(feats[k]) for k in FEATURE_NAMES]
        rows.append(row)
        ts.append(snap.timestamp)

    if not rows:
        return LOBFeatureSet(
            X=pd.DataFrame(columns=list(FEATURE_NAMES)),
            y=pd.Series(dtype=float),
            timestamps=pd.DatetimeIndex([]),
            feature_names=FEATURE_NAMES,
        )

    idx = pd.DatetimeIndex(ts)
    X = pd.DataFrame(rows, index=idx, columns=list(FEATURE_NAMES))
    y_raw = pd.Series(
        [
            rets[i]
            for i, snap in enumerate(snapshots)
            if rets[i] is not None
            and not (isinstance(rets[i], float) and np.isnan(rets[i]))
            and build_orderbook_features(snap, depth=depth).get("well_formed", 0.0) > 0
            and all(
                build_orderbook_features(snap, depth=depth).get(k) is not None
                for k in FEATURE_NAMES
                if k != "quote_staleness"
            )
        ],
        index=idx,
        dtype=float,
    )
    if classification:
        y = (y_raw > 0).astype(int).astype(float)
    else:
        y = y_raw

    return LOBFeatureSet(X=X, y=y, timestamps=idx, feature_names=FEATURE_NAMES)
