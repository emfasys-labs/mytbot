"""
Volatility regime strategy.

- Breakout when ATR expands with directional bar impulse.
- Mean reversion when ATR contracts and bar impulse fades.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Optional

import pandas as pd

from signals.engine import RawSignal
from strategies.base import Strategy


class VolatilityRegimeStrategy(Strategy):
    name = "volatility_regime"

    def _compute_target_notional(self, confidence: float, atr_ratio: float) -> dict[str, str]:
        try:
            base_notional = Decimal(str(self.config.get("base_target_notional", "4500")))
        except (InvalidOperation, TypeError, ValueError):
            base_notional = Decimal("4500")
        if base_notional <= 0:
            base_notional = Decimal("4500")
        conf_scale = Decimal(str(max(0.80, min(1.35, 0.75 + confidence * 0.65))))
        atr_scale = Decimal(str(max(0.75, min(1.30, 1.0 + (atr_ratio - 1.0) * 0.3))))
        target = (base_notional * conf_scale * atr_scale).quantize(Decimal("0.01"))
        return {
            "target_notional": str(target),
            "sizing_base_notional": str(base_notional.quantize(Decimal("0.01"))),
            "sizing_confidence_scale": str(conf_scale.quantize(Decimal("0.0001"))),
            "sizing_vol_regime_scale": str(atr_scale.quantize(Decimal("0.0001"))),
            "sizing_intent_source": "volatility_regime",
        }

    def generate_signal(self, symbol: str, features: pd.DataFrame) -> Optional[RawSignal]:
        if not self.enabled or features is None or features.empty or len(features) < 40:
            return None
        if "high" not in features.columns or "low" not in features.columns or "close" not in features.columns:
            return None

        lookback = int(self.config.get("atr_lookback", 14))
        atr_expansion = float(self.config.get("atr_expansion_ratio", 1.2))
        atr_compression = float(self.config.get("atr_compression_ratio", 0.85))
        min_bar_return = float(self.config.get("min_bar_return", 0.0015))

        df = features
        close = pd.to_numeric(df["close"], errors="coerce")
        high = pd.to_numeric(df["high"], errors="coerce")
        low = pd.to_numeric(df["low"], errors="coerce")
        prev_close = close.shift(1)
        tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
        atr_fast = tr.rolling(lookback).mean().iloc[-1]
        atr_slow = tr.rolling(lookback * 3).mean().iloc[-1]
        if pd.isna(atr_fast) or pd.isna(atr_slow) or float(atr_slow) <= 0:
            return None
        atr_ratio = float(atr_fast) / float(atr_slow)

        c0 = float(close.iloc[-2])
        c1 = float(close.iloc[-1])
        if c0 <= 0:
            return None
        bar_ret = (c1 - c0) / c0

        mode = None
        side = None
        if atr_ratio >= atr_expansion and abs(bar_ret) >= min_bar_return:
            mode = "vol_breakout"
            side = "buy" if bar_ret > 0 else "sell"
            confidence = min(0.58 + (atr_ratio - atr_expansion) * 0.22 + abs(bar_ret) * 8.0, 0.94)
        elif atr_ratio <= atr_compression and abs(bar_ret) <= min_bar_return * 0.8:
            mode = "vol_compression_revert"
            ema_fast = float(close.ewm(span=8, adjust=False).mean().iloc[-1])
            ema_slow = float(close.ewm(span=21, adjust=False).mean().iloc[-1])
            side = "sell" if ema_fast > ema_slow else "buy"
            confidence = min(0.54 + (atr_compression - atr_ratio) * 0.35, 0.84)
        else:
            return None

        md = self._compute_target_notional(confidence=float(confidence), atr_ratio=atr_ratio)
        return RawSignal(
            strategy=self.name,
            symbol=symbol,
            side=side,
            confidence=float(confidence),
            broker=self.preferred_broker,
            asset_class=self.asset_class,
            metadata={
                "vol_mode": mode,
                "atr_ratio": round(atr_ratio, 6),
                "bar_return": round(bar_ret, 6),
                **md,
            },
        )
