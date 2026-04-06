"""
Triple-barrier labels and simple meta-labeling helper.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

try:
    from sklearn.ensemble import RandomForestClassifier
except Exception:  # noqa: BLE001
    RandomForestClassifier = None


@dataclass
class TripleBarrierSpec:
    pt_mult: float = 2.0
    sl_mult: float = 1.5
    max_horizon: int = 10
    vol_window: int = 20


def triple_barrier_labels(close: pd.Series, spec: TripleBarrierSpec) -> pd.Series:
    """
    Label each timestamp:
    +1 profit barrier first, -1 stop barrier first, 0 if timeout/no hit.
    """
    px = close.astype(float).copy()
    ret = px.pct_change()
    vol = ret.rolling(spec.vol_window).std().fillna(ret.std() or 0.0)
    labels = pd.Series(0, index=px.index, dtype=int)
    n = len(px)
    for i in range(n):
        p0 = px.iloc[i]
        if p0 <= 0:
            continue
        sigma = float(vol.iloc[i] or 0.0)
        if sigma <= 0:
            continue
        up = p0 * (1.0 + spec.pt_mult * sigma)
        dn = p0 * max(0.000001, 1.0 - spec.sl_mult * sigma)
        j_end = min(n, i + spec.max_horizon + 1)
        future = px.iloc[i + 1 : j_end]
        if future.empty:
            continue
        up_hit = future[future >= up]
        dn_hit = future[future <= dn]
        up_idx = up_hit.index[0] if not up_hit.empty else None
        dn_idx = dn_hit.index[0] if not dn_hit.empty else None
        if up_idx is None and dn_idx is None:
            labels.iloc[i] = 0
        elif dn_idx is None:
            labels.iloc[i] = 1
        elif up_idx is None:
            labels.iloc[i] = -1
        else:
            labels.iloc[i] = 1 if up_idx <= dn_idx else -1
    return labels


def train_meta_label_model(
    features: pd.DataFrame,
    labels: pd.Series,
):
    """
    Train a simple binary meta-model: take trade (1) vs skip (0).
    This is intentionally conservative and deterministic.
    """
    if RandomForestClassifier is None:
        return None
    x = features.copy()
    y = labels.copy()
    common = x.index.intersection(y.index)
    if len(common) < 50:
        return None
    x = x.loc[common].replace([float("inf"), float("-inf")], pd.NA).fillna(0.0)
    # take-trade label: any non-zero directional outcome.
    yb = (y.loc[common] != 0).astype(int)
    if yb.nunique() < 2:
        return None
    clf = RandomForestClassifier(
        n_estimators=200,
        max_depth=5,
        min_samples_leaf=10,
        random_state=42,
        class_weight="balanced_subsample",
    )
    clf.fit(x, yb)
    return clf

