"""
strategies/mean_reversion.py
============================
Simple RSI + Bollinger mean reversion strategy for M3.
"""

from __future__ import annotations

import logging
from typing import Optional

import pandas as pd

from signals.engine import RawSignal
from strategies.base import Strategy

logger = logging.getLogger(__name__)


class MeanReversionStrategy(Strategy):
    """
    Buy when price is oversold and outside lower Bollinger Band.
    Sell when price is overbought and outside upper Bollinger Band.
    """

    name = "mean_reversion"

    def generate_signal(self, symbol: str, features: pd.DataFrame) -> Optional[RawSignal]:
        if not self.enabled or features is None or features.empty:
            return None
        try:
            return self._evaluate(symbol, features)
        except Exception as exc:  # noqa: BLE001
            logger.error("mean_reversion error on {}: {}", symbol, exc)
            return None

    def _evaluate(self, symbol: str, df: pd.DataFrame) -> Optional[RawSignal]:
        lookback = int(self.config.get("lookback_periods", 30))
        if len(df) < lookback:
            return None

        latest = df.iloc[-1]
        close = float(latest["close"])
        rsi = self._to_float(latest.get("rsi_14"))
        bb_lower = self._to_float(self._get_latest_band_value(df, latest, "BBL"))
        bb_upper = self._to_float(self._get_latest_band_value(df, latest, "BBU"))
        if rsi is None or bb_lower is None or bb_upper is None:
            return None

        rsi_buy = float(self.config.get("rsi_buy_threshold", 30.0))
        rsi_sell = float(self.config.get("rsi_sell_threshold", 70.0))
        band_epsilon = float(self.config.get("band_epsilon", 0.0))

        buy_setup = rsi <= rsi_buy and close <= bb_lower * (1.0 + band_epsilon)
        sell_setup = rsi >= rsi_sell and close >= bb_upper * (1.0 - band_epsilon)
        if not buy_setup and not sell_setup:
            return None

        side = "buy" if buy_setup else "sell"
        center = (bb_lower + bb_upper) / 2.0
        stretch = abs(close - center) / center if center > 0 else 0.0
        confidence = min(0.55 + stretch * 5.0, 0.95)
        atr = self._calculate_atr(df, lookback)
        atr_pct = float(atr / close) if close > 0 else 0.0

        return RawSignal(
            strategy=self.name,
            symbol=symbol,
            side=side,
            confidence=float(confidence),
            broker=self.preferred_broker,
            asset_class=self.asset_class,
            metadata={
                "close": close,
                "rsi_14": rsi,
                "bb_lower": bb_lower,
                "bb_upper": bb_upper,
                "stretch": stretch,
                "atr_pct": atr_pct,
            },
        )

    @staticmethod
    def _get_latest_band_value(df: pd.DataFrame, latest: pd.Series, band_prefix: str) -> object:
        """
        Resolve Bollinger column variants from feature engineering.
        We see names like:
          - BBL_20_2.0
          - BBL_20_2
          - BBL_20_2.0_2.0
        """
        direct_candidates = [
            f"{band_prefix}_20_2.0",
            f"{band_prefix}_20_2",
            f"{band_prefix}_20_2.0_2.0",
        ]
        for col in direct_candidates:
            if col in df.columns:
                return latest.get(col)

        # Generic fallback: first matching Bollinger column by prefix.
        for col in df.columns:
            if str(col).startswith(f"{band_prefix}_"):
                return latest.get(col)
        return None

    def _calculate_atr(self, df: pd.DataFrame, period: int) -> float:
        high = df["high"]
        low = df["low"]
        close = df["close"]
        prev_close = close.shift(1)
        tr = pd.concat(
            [high - low, (high - prev_close).abs(), (low - prev_close).abs()],
            axis=1,
        ).max(axis=1)
        return float(tr.rolling(period).mean().iloc[-1])

    @staticmethod
    def _to_float(value: object) -> Optional[float]:
        if value is None:
            return None
        try:
            out = float(value)
        except (TypeError, ValueError):
            return None
        if pd.isna(out):
            return None
        return out

