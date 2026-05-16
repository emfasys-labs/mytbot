from __future__ import annotations

import numpy as np
import pandas as pd

from scripts.train_phase_b_panel import build_phase_b_dataset


def test_build_phase_b_dataset_uses_production_feature_columns() -> None:
    idx = pd.date_range("2024-01-01", periods=160, freq="h", tz="UTC")
    close = pd.Series(np.linspace(100, 120, len(idx)), index=idx)
    frame = pd.DataFrame(
        {
            "close": close,
            "rsi_14": np.linspace(40, 60, len(idx)),
            "MACD_12_26_9": np.linspace(-1, 1, len(idx)),
            "atr_14": np.linspace(0.5, 1.5, len(idx)),
            "vol_ratio": np.ones(len(idx)),
            "fracdiff_0_4": close.pct_change().fillna(0.0),
            "mostly_missing": [None] * len(idx),
            "not_in_contract": np.arange(len(idx)),
        },
        index=idx,
    )

    ds, features, target = build_phase_b_dataset(
        frame,
        window=16,
        horizon=1,
        min_feature_coverage=0.8,
    )

    assert len(ds.y) > 100
    assert set(ds.feature_names) == {
        "rsi_14",
        "MACD_12_26_9",
        "atr_14",
        "vol_ratio",
        "fracdiff_0_4",
    }
    assert features.index.equals(target.index)
    assert ds.X.shape[1:] == (16, 5)
