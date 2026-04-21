"""M2 data.features unit tests."""

import numpy as np
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


def test_vol_ratio_is_one_when_feed_has_no_volume():
    """Forex bars from yfinance ship volume=0 on every bar.

    Before the fix, ``vol_ratio`` came out NaN, which pushed forex pairs
    to 0% full-feature completeness and starved the strategies. Now a
    structurally-volumeless feed should yield a neutral ``vol_ratio=1.0``
    so momentum / mean-reversion can still score those bars.
    """
    idx = pd.date_range("2024-01-01", periods=60, freq="D", tz="UTC")
    # Typical EURUSD=X yfinance snapshot: prices move, volume = 0 everywhere.
    df = pd.DataFrame(
        {
            "open": np.linspace(1.10, 1.15, 60),
            "high": np.linspace(1.11, 1.16, 60),
            "low":  np.linspace(1.09, 1.14, 60),
            "close": np.linspace(1.105, 1.155, 60),
            "volume": [0.0] * 60,
        },
        index=idx,
    )
    out = compute_feature_columns(df)
    # After the 20-bar warm-up the vol_ratio must be the neutral 1.0,
    # never NaN — otherwise the completeness gate rejects the series.
    tail = out["vol_ratio"].iloc[30:]
    assert tail.notna().all(), (
        f"vol_ratio must be non-NaN for volumeless feeds after warm-up, "
        f"got {tail.isna().sum()} NaNs"
    )
    assert (tail == 1.0).all(), (
        f"vol_ratio must be exactly 1.0 (neutral) for volumeless feeds, got {tail.unique()}"
    )


def test_vol_ratio_keeps_zero_volume_penalty_when_history_has_volume():
    """A single zero-volume bar inside a normally-trading symbol must
    still read as vol_ratio=0, not 1.0 — we only want the neutralisation
    when the whole series is structurally volumeless.
    """
    idx = pd.date_range("2024-01-01", periods=60, freq="D", tz="UTC")
    volumes = [1_000_000.0] * 60
    volumes[-1] = 0.0  # final bar: no trades
    df = pd.DataFrame(
        {
            "open":  np.linspace(100, 120, 60),
            "high":  np.linspace(101, 121, 60),
            "low":   np.linspace(99,  119, 60),
            "close": np.linspace(100.5, 120.5, 60),
            "volume": volumes,
        },
        index=idx,
    )
    out = compute_feature_columns(df)
    # last bar: real drought, should be 0 (not 1.0).
    assert out["vol_ratio"].iloc[-1] == 0.0
    # warm-up tail is normal (~1.0 since volumes are constant).
    assert out["vol_ratio"].iloc[-2] == 1.0


def test_vol_ratio_normal_series_unchanged():
    """Regression check: the fix must not touch the normal code path."""
    idx = pd.date_range("2024-01-01", periods=60, freq="D", tz="UTC")
    # Double the volume on the last bar — vol_ratio should be ~2.0.
    volumes = [1_000_000.0] * 59 + [2_000_000.0]
    df = pd.DataFrame(
        {
            "open":  np.linspace(100, 120, 60),
            "high":  np.linspace(101, 121, 60),
            "low":   np.linspace(99,  119, 60),
            "close": np.linspace(100.5, 120.5, 60),
            "volume": volumes,
        },
        index=idx,
    )
    out = compute_feature_columns(df)
    # vol_sma_20 on last bar includes 19 bars of 1M and 1 bar of 2M → mean = 1.05M.
    assert abs(out["vol_ratio"].iloc[-1] - (2_000_000.0 / 1_050_000.0)) < 1e-9
