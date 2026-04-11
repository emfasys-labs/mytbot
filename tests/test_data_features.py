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
    assert any(c.startswith("BBL_") for c in out.columns)
    assert any(c.startswith("BBU_") for c in out.columns)
    assert "fracdiff_0_4" in out.columns
    assert "hurst_dfa_128" in out.columns
    assert "garch_vol_1d" in out.columns
    assert "vpin_proxy_50" in out.columns
    assert "volume_z" in out.columns
    assert "relative_dollar_volume" in out.columns
    assert "trade_count_anomaly" in out.columns
    assert "volume_persistence" in out.columns
    assert "fake_spike_penalty" in out.columns
    last = out.iloc[-1]
    js = row_features_to_json_dict(last)
    assert "rsi_14" in js or any(k.startswith("MACD") for k in js)
    assert "volume_z" in js
