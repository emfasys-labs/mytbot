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

# Stable keys written to feature_snapshots.features (D015 volume/flow).
VOLUME_FLOW_KEYS = frozenset(
    {
        "volume_z",
        "relative_dollar_volume",
        "trade_count_anomaly",
        "volume_persistence",
        "fake_spike_penalty",
    }
)
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


def _volume_flow_defaults() -> dict[str, Any]:
    return {
        "volume_z_window": 60,
        "volume_z_min_periods": 20,
        "dollar_volume_sma": 20,
        "persistence_lookback": 5,
        "persistence_vol_ratio_threshold": 1.0,
        "persistence_volume_z_threshold": 0.5,
        "trade_count_proxy_window": 20,
        "fake_spike_vol_ratio": 2.5,
        "fake_spike_abs_return_max": 0.002,
        "fake_spike_vpin_low": 0.15,
    }


def _compute_volume_flow_columns(x: pd.DataFrame, cfg: dict[str, Any] | None) -> pd.DataFrame:
    """
    D015 volume/flow features for yfinance OHLCV bars. ``orderbook_imbalance`` is not
    computed here (no L2). ``trade_count_anomaly`` is a bar-activity proxy until tick data exists.
    """
    c = _volume_flow_defaults()
    if cfg:
        c.update({k: cfg[k] for k in c if k in cfg})

    close = x["close"].astype(float)
    vol = x["volume"].astype(float)
    high = x["high"].astype(float)
    low = x["low"].astype(float)

    wz = int(c["volume_z_window"])
    minp = int(c["volume_z_min_periods"])
    log_v = np.log1p(np.maximum(vol, 0.0))
    lv_mean = pd.Series(log_v, index=x.index).rolling(wz, min_periods=minp).mean()
    lv_std = pd.Series(log_v, index=x.index).rolling(wz, min_periods=minp).std().replace(0.0, np.nan)
    x["volume_z"] = (log_v - lv_mean) / lv_std

    dv = close * vol
    dvn = int(c["dollar_volume_sma"])
    dv_sma = pd.Series(dv, index=x.index).rolling(dvn, min_periods=max(5, dvn // 4)).mean()
    x["relative_dollar_volume"] = np.where(
        (dv_sma.isna()) | (dv_sma <= 0),
        np.nan,
        np.clip(dv / dv_sma, 0.0, 10.0),
    )

    # Activity proxy: normalized bar range × volume z (no tick counts in yfinance).
    tw = int(c["trade_count_proxy_window"])
    bar_range = (high - low) / close.replace(0.0, np.nan)
    br_mean = bar_range.rolling(tw, min_periods=max(5, tw // 4)).mean()
    br_std = bar_range.rolling(tw, min_periods=max(5, tw // 4)).std().replace(0.0, np.nan)
    range_z = (bar_range - br_mean) / br_std
    vol_z_tc = x["volume_z"]
    x["trade_count_anomaly"] = np.clip(range_z * np.nan_to_num(vol_z_tc, nan=0.0) / 4.0, -3.0, 3.0)

    pl = int(c["persistence_lookback"])
    vrt = float(c["persistence_vol_ratio_threshold"])
    vzt = float(c["persistence_volume_z_threshold"])
    elevated = (
        (x["vol_ratio"] > vrt) | (x["volume_z"] > vzt)
    ).astype(float)
    x["volume_persistence"] = elevated.rolling(pl, min_periods=1).mean().clip(0.0, 1.0)

    abs_ret = close.pct_change().abs()
    vpin = x["vpin_proxy_50"] if "vpin_proxy_50" in x.columns else pd.Series(0.0, index=x.index)
    spike_vr = float(c["fake_spike_vol_ratio"])
    spike_ret = float(c["fake_spike_abs_return_max"])
    spike_vp = float(c["fake_spike_vpin_low"])
    fake = np.where(
        (x["vol_ratio"] > spike_vr) & (abs_ret < spike_ret) & (vpin < spike_vp),
        0.65,
        np.where((x["vol_ratio"] > spike_vr * 1.1) & (abs_ret < spike_ret * 1.5), 0.35, 0.0),
    )
    x["fake_spike_penalty"] = fake
    return x


def compute_feature_columns(df: pd.DataFrame, pipeline_cfg: dict[str, Any] | None = None) -> pd.DataFrame:
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
    # ``vol_ratio`` = current bar volume / rolling 20-bar mean volume.
    # Edge cases:
    #   1. ``vol_sma_20`` NaN — not enough bars yet → NaN (early warm-up).
    #   2. ``vol_sma_20 == 0`` and current ``vol == 0`` — the feed
    #      structurally has no volume (yfinance forex pairs report
    #      0 volume on every bar). Treat as the neutral value 1.0 so
    #      downstream strategies don't refuse to fire on volumeless
    #      instruments. Without this, forex pairs had 0.0% feature
    #      completeness and produced zero signals despite having
    #      perfectly good OHLC history.
    #   3. ``vol_sma_20 == 0`` and current ``vol > 0`` — unusual,
    #      keep NaN so we don't divide by zero or fabricate a spike.
    vol_sma = x["vol_sma_20"]
    x["vol_ratio"] = np.where(
        vol_sma.isna(),
        np.nan,
        np.where(
            vol_sma == 0,
            np.where(vol == 0, 1.0, np.nan),
            vol / vol_sma.replace(0, np.nan),
        ),
    )
    # VPIN proxy before volume_flow (fake_spike_penalty); recomputed in _compute_research_columns for consistency.
    direction = np.sign(close.diff().fillna(0.0))
    buy_vol = (vol * (direction > 0)).rolling(50).sum()
    sell_vol = (vol * (direction < 0)).rolling(50).sum()
    total_bs = (buy_vol + sell_vol).replace(0.0, np.nan)
    x["vpin_proxy_50"] = ((buy_vol - sell_vol).abs() / total_bs).clip(0.0, 1.0)
    vf_cfg = (pipeline_cfg or {}).get("volume_flow_features") or {}
    x = _compute_volume_flow_columns(x, vf_cfg)
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
