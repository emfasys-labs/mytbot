"""
strategies/trend_breakout.py
============================
Trend-following channel breakout — the "sniper / shotgun" weapon (D158).

The opposite philosophy to mean-reversion: instead of fading a move, it RIDES
one. Enters when price breaks out of its recent range (a new N-bar extreme),
betting the move continues — the classic Donchian / Turtle edge that underpins
most managed-futures (CTA) returns. It aims for LARGE moves over many bars, so
the per-trade target dwarfs transaction costs (unlike intraday scalps, where
the toll eats the prize).

Bidirectional: long on an N-bar high breakout, short on an N-bar low breakdown.
The position is meant to be held while the trend persists; exit logic in the
backtest harness / live loop closes on the opposite signal or a stop.

Config (config/strategies.yaml::strategies.trend_breakout):
    enabled               bool
    entry_lookback        int    bars for the breakout channel (e.g. 50)
    atr_lookback          int    bars for ATR (volatility filter / sizing)
    atr_min_pct           float  skip dead markets (ATR% floor)
    min_breakout_atr      float  breakout must clear the channel by this × ATR
                                 (filters marginal pokes through the band)
    base_target_notional  Decimal-as-string sizing intent
"""

from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

import pandas as pd

from signals.engine import RawSignal
from strategies.base import Strategy

logger = logging.getLogger(__name__)


class TrendBreakoutStrategy(Strategy):
    name = "trend_breakout"

    def _params(self) -> Optional[dict[str, Any]]:
        cfg = self.effective_config()
        try:
            return {
                "entry_lookback": int(cfg.get("entry_lookback", 50)),
                "atr_lookback": int(cfg.get("atr_lookback", 20)),
                "atr_min_pct": float(cfg.get("atr_min_pct", 0.0)),
                "min_breakout_atr": float(cfg.get("min_breakout_atr", 0.5)),
                "base_notional": cfg.get("base_target_notional", "20000"),
            }
        except (TypeError, ValueError):
            return None

    def generate_signal(self, symbol: str, features: pd.DataFrame) -> Optional[RawSignal]:
        if not self.enabled or features is None or features.empty:
            return None
        p = self._params()
        if p is None:
            return None
        lookback = p["entry_lookback"]
        if len(features) < lookback + 2:
            return None
        try:
            df = features
            latest = df.iloc[-1]
            prev = df.iloc[:-1]
            close = float(latest["close"])
            # Channel of the *prior* bars (exclude the current bar so a
            # breakout is a genuine new extreme, not self-referential).
            chan_high = float(prev["high"].rolling(lookback).max().iloc[-1])
            chan_low = float(prev["low"].rolling(lookback).min().iloc[-1])
            atr = self._atr(df, p["atr_lookback"])
            atr_pct = (atr / close) if close > 0 else 0.0
            if atr <= 0 or close <= 0:
                return None
            if p["atr_min_pct"] > 0 and atr_pct < p["atr_min_pct"]:
                return None

            buffer = Decimal(str(p["min_breakout_atr"])) * Decimal(str(atr))
            up = Decimal(str(close)) > Decimal(str(chan_high)) + buffer
            dn = Decimal(str(close)) < Decimal(str(chan_low)) - buffer
            if not (up or dn):
                return None

            side = "buy" if up else "sell"
            if up:
                strength = (close - chan_high) / chan_high if chan_high > 0 else 0.0
            else:
                strength = (chan_low - close) / chan_low if chan_low > 0 else 0.0
            confidence = min(0.55 + max(0.0, strength) * 8.0, 0.92)

            target = self._target_notional(p["base_notional"], confidence, atr_pct)
            if target is None:
                return None
            return RawSignal(
                strategy=self.name,
                symbol=symbol,
                side=side,
                confidence=float(confidence),
                broker=self.preferred_broker,
                asset_class=self.asset_class,
                metadata={
                    "weapon_class": "sniper",
                    "entry_lookback": lookback,
                    "channel_high": chan_high,
                    "channel_low": chan_low,
                    "close": close,
                    "atr_pct": atr_pct,
                    "breakout_strength": float(strength),
                    "target_notional": target,
                },
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("trend_breakout error on %s: %s", symbol, exc)
            return None

    @staticmethod
    def _atr(df: pd.DataFrame, period: int) -> float:
        high, low, close = df["high"], df["low"], df["close"]
        prev_close = close.shift(1)
        tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
        v = tr.rolling(period).mean().iloc[-1]
        return float(v) if v == v else 0.0  # NaN guard

    @staticmethod
    def _target_notional(base_raw: Any, confidence: float, atr_pct: float) -> Optional[str]:
        try:
            base = Decimal(str(base_raw))
        except (InvalidOperation, TypeError, ValueError):
            return None
        if base <= 0:
            return None
        conf = max(0.0, min(1.0, confidence))
        conf_scale = Decimal(str(0.75 + 0.5 * conf))
        atr = max(float(atr_pct), 0.0)
        vol_scale = Decimal(str(max(0.70, min(1.30, 0.02 / atr)))) if atr > 0 else Decimal("1.0")
        gross = base * conf_scale * vol_scale
        target = max(base * Decimal("0.5"), min(base * Decimal("1.5"), gross))
        return str(target.quantize(Decimal("0.01")))
