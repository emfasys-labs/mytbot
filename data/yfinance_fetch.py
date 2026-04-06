"""
Download OHLCV from yfinance (blocking); call via asyncio.to_thread from async code.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import yfinance as yf


def fetch_history(
    symbol: str,
    *,
    interval: str,
    period: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
) -> pd.DataFrame:
    """
    Returns a DataFrame with DatetimeIndex (UTC) and columns
    Open, High, Low, Close, Volume (yfinance defaults).
    """
    t = yf.Ticker(symbol.strip())
    if period:
        df = t.history(period=period, interval=interval, auto_adjust=True)
    elif start is not None:
        df = t.history(
            start=start.date(),
            end=end.date() if end else None,
            interval=interval,
            auto_adjust=True,
        )
    else:
        raise ValueError("either period= or start= is required")

    if df is None or df.empty:
        return pd.DataFrame()

    df = df[~df.index.duplicated(keep="last")]
    idx = df.index
    if idx.tz is None:
        df.index = idx.tz_localize(timezone.utc)
    else:
        df.index = idx.tz_convert(timezone.utc)
    return df
