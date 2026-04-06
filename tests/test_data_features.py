"""M2 data.features unit tests."""

import pandas as pd

from data.features import compute_feature_columns, row_features_to_json_dict


def test_compute_features_adds_indicators():
    idx = pd.date_range("2024-01-01", periods=60, freq="D", tz="UTC")
    df = pd.DataFrame(
        {
            "open": range(100, 160),
            "high": range(105, 165),
            "low": range(95, 155),
            "close": range(102, 162),
            "volume": [1_000_000.0] * 60,
        },
        index=idx,
    )
    out = compute_feature_columns(df)
    assert "rsi_14" in out.columns
    assert "atr_14" in out.columns
    assert "mom_10" in out.columns
    last = out.iloc[-1]
    js = row_features_to_json_dict(last)
    assert "rsi_14" in js or any(k.startswith("MACD") for k in js)
