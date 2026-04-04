"""
strategies/momentum.py
=======================
Momentum Breakout Strategy — the first strategy to implement (M3).

Logic:
- Price breaks above N-period rolling high with confirming volume spike
- Volatility within acceptable range (not too quiet, not too explosive)
- Signal: BUY on breakout, EXIT on momentum fade or stop hit

This is the simplest, most debuggable strategy.
Every signal it generates has a clear, explainable reason.

Config (from config/strategies.yaml):
    lookback_periods: 20        # rolling high window
    volume_multiplier: 1.5      # volume must be 1.5x average
    atr_min: 0.005              # min volatility (avoid dead markets)
    atr_max: 0.05               # max volatility (avoid chaos)
    momentum_threshold: 0.002   # min price move above rolling high
"""

from typing import Optional
import pandas as pd
import logging

from strategies.base import Strategy
from signals.engine import RawSignal

logger = logging.getLogger(__name__)


class MomentumBreakoutStrategy(Strategy):
    """
    Momentum breakout on liquid assets.
    Suitable for: US large-cap equities, BTC, ETH, major ETFs.
    Timeframe: 5-minute to 1-hour candles.
    """

    name = "momentum_breakout"

    def generate_signal(
        self,
        symbol: str,
        features: pd.DataFrame,
    ) -> Optional[RawSignal]:
        """
        Generate a BUY signal when price breaks above rolling high with volume.
        Returns None if no breakout detected.
        """

        if not self.enabled:
            return None

        if len(features) < self.config.get("lookback_periods", 20) + 1:
            logger.debug(f"{symbol}: not enough data ({len(features)} rows)")
            return None

        try:
            signal = self._evaluate(symbol, features)
            if signal:
                logger.info(f"SIGNAL {self.name} | {symbol} {signal.side} | confidence={signal.confidence:.2f}")
            return signal

        except Exception as e:
            logger.error(f"{self.name} error on {symbol}: {e}")
            return None

    def _evaluate(self, symbol: str, df: pd.DataFrame) -> Optional[RawSignal]:

        lookback   = self.config.get("lookback_periods", 20)
        vol_mult   = self.config.get("volume_multiplier", 1.5)
        atr_min    = self.config.get("atr_min", 0.005)
        atr_max    = self.config.get("atr_max", 0.05)
        mom_thresh = self.config.get("momentum_threshold", 0.002)

        latest       = df.iloc[-1]
        prev         = df.iloc[:-1]

        close        = latest["close"]
        volume       = latest["volume"]
        rolling_high = prev["high"].rolling(lookback).max().iloc[-1]
        avg_volume   = prev["volume"].rolling(lookback).mean().iloc[-1]

        # ATR-based volatility check
        atr = self._calculate_atr(df, lookback)
        atr_pct = atr / close if close > 0 else 0

        # ── Breakout conditions ───────────────────────────────────────────────

        # 1. Price breaks above rolling high
        price_breakout = close > rolling_high * (1 + mom_thresh)

        # 2. Volume confirms the move
        volume_confirms = volume > avg_volume * vol_mult

        # 3. Volatility is in acceptable range
        volatility_ok = atr_min <= atr_pct <= atr_max

        # ── Combine ───────────────────────────────────────────────────────────

        if not (price_breakout and volume_confirms and volatility_ok):
            return None

        # Confidence: 0.5 base + bonus for how clean the breakout is
        breakout_strength = (close - rolling_high) / rolling_high
        volume_strength   = min((volume / avg_volume) / 3, 0.3)
        confidence        = min(0.5 + breakout_strength * 10 + volume_strength, 0.95)

        return RawSignal(
            strategy=self.name,
            symbol=symbol,
            side="buy",
            confidence=float(confidence),
            broker=self.preferred_broker,
            asset_class=self.asset_class,
            metadata={
                "rolling_high":       float(rolling_high),
                "close":              float(close),
                "breakout_strength":  float(breakout_strength),
                "volume_ratio":       float(volume / avg_volume),
                "atr_pct":            float(atr_pct),
                "lookback":           lookback,
            },
        )

    def _calculate_atr(self, df: pd.DataFrame, period: int) -> float:
        """Average True Range — measures volatility."""
        high   = df["high"]
        low    = df["low"]
        close  = df["close"]
        prev_close = close.shift(1)

        tr = pd.concat([
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ], axis=1).max(axis=1)

        return float(tr.rolling(period).mean().iloc[-1])
