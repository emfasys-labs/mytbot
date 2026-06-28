"""
Download OHLCV from yfinance (blocking); call via asyncio.to_thread from async code.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import pandas as pd
import yfinance as yf


_EXPECTED_NO_DATA_MESSAGES = (
    "possibly delisted; no price data found",
    "no timezone found, symbol may be delisted",
)


class _ExpectedNoDataFilter(logging.Filter):
    """Keep normal discovery misses out of the application error stream."""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage().lower()
        return not any(fragment in message for fragment in _EXPECTED_NO_DATA_MESSAGES)


def _install_expected_no_data_filter() -> None:
    for logger_name in ("yfinance", "yfinance.scrapers.history"):
        yf_logger = logging.getLogger(logger_name)
        if not any(isinstance(item, _ExpectedNoDataFilter) for item in yf_logger.filters):
            yf_logger.addFilter(_ExpectedNoDataFilter())


_install_expected_no_data_filter()


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
