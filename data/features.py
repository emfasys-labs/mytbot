"""
Technical features via pandas-ta (RSI, MACD, ATR, momentum, volume metrics).
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import numpy as np
import pandas as pd
import pandas_ta as ta


def _ohlcv_lower(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    rename = {}
    for c in out.columns:
        rename[c] = c.lower()
    out = out.rename(columns=rename)
    for col in ("open", "high", "low", "close", "volume"):
        if col not in out.columns:
            raise ValueError(f"expected column {col}, got {list(out.columns)}")
    return out


def compute_feature_columns(df: pd.DataFrame) -> pd.DataFrame:
    """
    Append indicator columns to OHLCV DataFrame (lowercase open/high/low/close/volume).
    Index preserved (typically DatetimeIndex).
    """
    x = _ohlcv_lower(df)
    close = x["close"]
    high = x["high"]
    low = x["low"]
    vol = x["volume"]

    x["rsi_14"] = ta.rsi(close, length=14)
    macd = ta.macd(close)
    if macd is not None and not macd.empty:
        for c in macd.columns:
            x[c] = macd[c]
    x["atr_14"] = ta.atr(high, low, close, length=14)
    x["mom_10"] = ta.mom(close, length=10)
    x["vol_sma_20"] = ta.sma(vol, length=20)
    x["vol_ratio"] = np.where(
        (x["vol_sma_20"].isna()) | (x["vol_sma_20"] == 0),
        np.nan,
        vol / x["vol_sma_20"],
    )
    return x


def row_features_to_json_dict(row: pd.Series) -> dict[str, Any]:
    """JSON-serialisable feature dict for one bar (NaN -> None)."""
    skip = {"open", "high", "low", "close", "volume"}
    out: dict[str, Any] = {}
    for k, v in row.items():
        kl = str(k).lower()
        if kl in skip:
            continue
        if pd.isna(v):
            out[str(k)] = None
        elif isinstance(v, (np.floating, np.integer)):
            out[str(k)] = v.item()
        elif isinstance(v, Decimal):
            out[str(k)] = str(v)
        elif isinstance(v, float):
            out[str(k)] = v
        else:
            out[str(k)] = float(v) if isinstance(v, (int, float)) else str(v)
    return out


def features_json_dumps(features: dict[str, Any]) -> str:
    return json.dumps(features, separators=(",", ":"), sort_keys=True)
