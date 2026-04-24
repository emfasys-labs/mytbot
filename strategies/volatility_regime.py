"""
Volatility regime strategy.

- Breakout when ATR expands with directional bar impulse.
- Mean reversion when ATR contracts and bar impulse fades.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Optional

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

    def no_setup_snapshot(self, symbol: str, features: pd.DataFrame) -> dict[str, Any]:
        """ATR fast/slow ratio + bar return near-miss when :meth:`generate_signal` is None."""
        out: dict[str, Any] = {"near_miss_kind": "volatility_regime"}
        if not self.enabled or features is None or features.empty:
            out["near_miss_primary"] = "no_data"
            return out
        if "high" not in features.columns or "low" not in features.columns or "close" not in features.columns:
            out["near_miss_primary"] = "missing_ohlcv"
            return out
        n = len(features)
        out["rows_available"] = n
        lookback = int(self.config.get("atr_lookback", 14))
        atr_expansion = float(self.config.get("atr_expansion_ratio", 1.2))
        atr_compression = float(self.config.get("atr_compression_ratio", 0.85))
        min_bar_return = float(self.config.get("min_bar_return", 0.0015))
        out["expansion_threshold"] = atr_expansion
        out["compression_threshold"] = atr_compression
        out["min_bar_return"] = min_bar_return
        if n < 40:
            out["near_miss_primary"] = "insufficient_rows"
            return out
        try:
            close = pd.to_numeric(features["close"], errors="coerce")
            high = pd.to_numeric(features["high"], errors="coerce")
            low = pd.to_numeric(features["low"], errors="coerce")
            prev_close = close.shift(1)
            tr = pd.concat(
                [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
            ).max(axis=1)
            atr_fast = float(tr.rolling(lookback).mean().iloc[-1])
            atr_slow = float(tr.rolling(lookback * 3).mean().iloc[-1])
            out["atr_fast"] = round(atr_fast, 10) if atr_fast == atr_fast else 0.0
            out["atr_slow"] = round(atr_slow, 10) if atr_slow == atr_slow else 0.0
            if not (atr_fast == atr_fast) or not (atr_slow == atr_slow) or float(atr_slow) <= 0:
                out["near_miss_primary"] = "invalid_atr"
                return out
            atr_ratio = float(atr_fast) / float(atr_slow)
            out["atr_ratio"] = round(atr_ratio, 6)
            c0 = float(close.iloc[-2])
            c1 = float(close.iloc[-1])
            if c0 <= 0:
                out["near_miss_primary"] = "invalid_close"
                return out
            bar_ret = (c1 - c0) / c0
            out["bar_return_abs"] = round(abs(bar_ret), 8)
            in_exp = atr_ratio >= atr_expansion and abs(bar_ret) >= min_bar_return
            in_comp = atr_ratio <= atr_compression and abs(bar_ret) <= min_bar_return * 0.8
            if in_exp:
                out["trigger_type"] = "vol_breakout"
            elif in_comp:
                out["trigger_type"] = "vol_compression_revert"
            else:
                out["trigger_type"] = "none"
            if atr_ratio >= atr_expansion and abs(bar_ret) < min_bar_return:
                out["near_miss_primary"] = "bar_impulse_too_weak_for_expansion"
            elif atr_ratio <= atr_compression and abs(bar_ret) > min_bar_return * 0.8:
                out["near_miss_primary"] = "bar_too_active_for_compression"
            elif atr_ratio < atr_expansion and atr_ratio > atr_compression:
                out["near_miss_primary"] = "in_mid_band"
            else:
                out["near_miss_primary"] = "no_clear_regime"
            out["reason_detail"] = "ATR fast/slow vs bar impulse"
        except Exception as exc:  # noqa: BLE001
            out["near_miss_primary"] = "diagnostic_error"
            out["reason_detail"] = str(exc)[:500]
        return out

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
