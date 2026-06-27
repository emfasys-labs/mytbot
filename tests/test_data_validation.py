"""M2 data.validation unit tests."""

from datetime import datetime, timedelta, timezone

import pandas as pd

from data.validation import validate_fetched_timestamps, validate_ohlcv_frame


def test_validate_ohlcv_ok_daily_synthetic():
    idx = pd.date_range("2024-01-01", periods=5, freq="D", tz="UTC")
    df = pd.DataFrame(
        {
            "open": [100, 101, 102, 103, 104],
            "high": [105, 106, 107, 108, 109],
            "low": [99, 100, 101, 102, 103],
            "close": [102, 103, 104, 105, 106],
            "volume": [1e6] * 5,
        },
        index=idx,
    )
    r = validate_ohlcv_frame(
        df,
        expected_interval=timedelta(days=1),
        stale_after=timedelta(days=30),
        now_utc=datetime(2024, 1, 10, tzinfo=timezone.utc),
    )
    assert r.ok
    assert not r.issues


def test_validate_ohlcv_high_below_low():
    idx = pd.date_range("2024-01-01", periods=2, freq="D", tz="UTC")
    df = pd.DataFrame(
        {
            "open": [100, 100],
            "high": [98, 101],
            "low": [99, 100],
            "close": [99, 100],
            "volume": [1, 1],
        },
        index=idx,
    )
    r = validate_ohlcv_frame(df, expected_interval=None)
    assert not r.ok
    assert any("high_less_than_low" in x for x in r.issues)


def test_validate_ohlcv_non_finite_prices():
    idx = pd.date_range("2024-01-01", periods=2, freq="D", tz="UTC")
    df = pd.DataFrame(
        {
            "open": [100.0, float("nan")],
            "high": [101.0, float("nan")],
            "low": [99.0, float("nan")],
            "close": [100.5, float("nan")],
            "volume": [1.0, 2.0],
        },
        index=idx,
    )

    r = validate_ohlcv_frame(df, expected_interval=None)

    assert not r.ok
    assert "non_finite_ohlcv_rows:1" in r.issues


def test_validate_future_timestamp_rejected():
    now = datetime(2024, 6, 1, tzinfo=timezone.utc)
    future = datetime(2099, 1, 1, tzinfo=timezone.utc)
    r = validate_fetched_timestamps([future], now_utc=now)
    assert not r.ok
