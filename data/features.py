"""
Technical + research features for M2:
- RSI, MACD, ATR, momentum, volume metrics
- Bollinger Bands
- Fractional differencing (or safe fallback)
- Hurst exponent regime proxy
- Volatility forecast proxy (GARCH where available)
- VPIN-style toxicity proxy
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import numpy as np
import pandas as pd
import pandas_ta as ta

try:  # Optional research dependency.
    import nolds  # type: ignore
except Exception:  # noqa: BLE001
    nolds = None

try:  # Optional research dependency.
    from arch import arch_model  # type: ignore
except Exception:  # noqa: BLE001
    arch_model = None

try:  # Optional research dependency.
    from fracdiff import fdiff  # type: ignore
except Exception:  # noqa: BLE001
    fdiff = None


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


def _compute_research_columns(x: pd.DataFrame) -> pd.DataFrame:
    close = x["close"].astype(float)
    vol = x["volume"].astype(float)
    rets = close.pct_change().fillna(0.0)

    # Fractional differencing fallback to returns if library unavailable.
    if fdiff is not None:
        try:
            frac = fdiff(close.values.astype(float), n=0.4, axis=0)
            x["fracdiff_0_4"] = pd.Series(frac, index=x.index)
        except Exception:  # noqa: BLE001
            x["fracdiff_0_4"] = rets
    else:
        x["fracdiff_0_4"] = rets

    # Hurst exponent (rolling window), fallback to NaN if unavailable.
    hurst = pd.Series(np.nan, index=x.index, dtype=float)
    if nolds is not None and len(x) >= 128:
        window = 128
        for i in range(window - 1, len(x)):
            arr = close.iloc[i - window + 1 : i + 1].values
            try:
                hurst.iloc[i] = float(nolds.dfa(arr))
            except Exception:  # noqa: BLE001
                hurst.iloc[i] = np.nan
    x["hurst_dfa_128"] = hurst

    # GARCH-style one-step vol forecast; fallback to rolling vol.
    garch_vol = pd.Series(np.nan, index=x.index, dtype=float)
    if arch_model is not None and len(x) >= 64:
        try:
            am = arch_model((rets * 100.0).dropna(), vol="GARCH", p=1, q=1, dist="t")
            res = am.fit(disp="off")
            cond_vol = res.conditional_volatility / 100.0
            garch_vol.loc[cond_vol.index] = cond_vol.values
        except Exception:  # noqa: BLE001
            garch_vol = rets.rolling(20).std()
    else:
        garch_vol = rets.rolling(20).std()
    x["garch_vol_1d"] = garch_vol

    # Lightweight VPIN proxy: rolling signed volume imbalance ratio.
    direction = np.sign(close.diff().fillna(0.0))
    buy_vol = (vol * (direction > 0)).rolling(50).sum()
    sell_vol = (vol * (direction < 0)).rolling(50).sum()
    total = (buy_vol + sell_vol).replace(0.0, np.nan)
    x["vpin_proxy_50"] = ((buy_vol - sell_vol).abs() / total).clip(0.0, 1.0)
    return x


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
    bb = ta.bbands(close, length=20, std=2.0)
    if bb is not None and not bb.empty:
        for c in bb.columns:
            x[c] = bb[c]
    x["vol_ratio"] = np.where(
        (x["vol_sma_20"].isna()) | (x["vol_sma_20"] == 0),
        np.nan,
        vol / x["vol_sma_20"],
    )
    x = _compute_research_columns(x)
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
