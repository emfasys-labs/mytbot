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

from typing import Any, Optional
from decimal import Decimal, InvalidOperation
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

    def _compute_target_notional(self, *, confidence: float, atr_pct: float) -> dict[str, str]:
        """D032: strategy-level sizing intent (confidence + volatility aware).

        Emits an absolute ``target_notional`` so downstream coordinator/execution
        layers can respect strategy intent without falling back to NAV defaults.
        """
        cfg = self.effective_config()
        try:
            base_notional = Decimal(str(cfg.get("base_target_notional", "5000")))
        except (InvalidOperation, TypeError, ValueError):
            base_notional = Decimal("5000")
        if base_notional <= 0:
            base_notional = Decimal("5000")

        # Confidence scaling: bounded around 1.0.
        conf = max(0.0, min(1.0, float(confidence)))
        conf_scale = Decimal(str(0.75 + 0.5 * conf))  # 0.75x .. 1.25x

        # Volatility scaling: lower ATR% allows modestly larger notional.
        try:
            atr = max(float(atr_pct), 0.0)
        except (TypeError, ValueError):
            atr = 0.0
        if atr > 0:
            raw_vol_scale = 0.02 / atr
            vol_scale = Decimal(str(max(0.70, min(1.30, raw_vol_scale))))
        else:
            vol_scale = Decimal("1.0")

        # Clamp final target around base to avoid runaway values.
        gross = base_notional * conf_scale * vol_scale
        min_notional = base_notional * Decimal("0.50")
        max_notional = base_notional * Decimal("1.50")
        target = max(min_notional, min(max_notional, gross)).quantize(Decimal("0.01"))

        return {
            "target_notional": str(target),
            "sizing_base_notional": str(base_notional.quantize(Decimal("0.01"))),
            "sizing_confidence_scale": str(conf_scale.quantize(Decimal("0.0001"))),
            "sizing_volatility_scale": str(vol_scale.quantize(Decimal("0.0001"))),
            "sizing_intent_source": "strategy_confidence_volatility",
        }

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

        cfg = self.effective_config()
        if len(features) < cfg.get("lookback_periods", 20) + 1:
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

    def no_setup_snapshot(self, symbol: str, df: pd.DataFrame) -> dict[str, Any]:
        """When :meth:`generate_signal` would return None, log near-miss diagnostics (D043)."""
        out: dict[str, Any] = {
            "near_miss_kind": "momentum_breakout",
        }
        if not self.enabled:
            out["near_miss_primary"] = "strategy_disabled"
            return out
        cfg = self.effective_config()
        lookback = int(cfg.get("lookback_periods", 20))
        if df is None or len(df) < lookback + 1:
            out["near_miss_primary"] = "insufficient_rows"
            out["rows_available"] = 0 if df is None or df.empty else len(df)
            out["min_rows"] = lookback + 1
            return out
        try:
            vol_mult = cfg.get("volume_multiplier", 1.5)
            atr_min = cfg.get("atr_min", 0.005)
            atr_max = cfg.get("atr_max", 0.05)
            mom_thresh = cfg.get("momentum_threshold", 0.002)
            latest = df.iloc[-1]
            prev = df.iloc[:-1]
            close = float(latest["close"])
            volume = float(latest["volume"])
            rolling_high = float(prev["high"].rolling(lookback).max().iloc[-1])
            avg_volume = float(prev["volume"].rolling(lookback).mean().iloc[-1])
            atr = self._calculate_atr(df, lookback)
            atr_pct = float(atr / close) if close > 0 else 0.0
            price_breakout = close > rolling_high * (1 + float(mom_thresh))
            volume_confirms = volume > avg_volume * float(vol_mult)
            volatility_ok = float(atr_min) <= atr_pct <= float(atr_max)
            out["price_breakout"] = bool(price_breakout)
            out["volume_confirms"] = bool(volume_confirms)
            out["volatility_ok"] = bool(volatility_ok)
            out["close"] = round(close, 8)
            out["rolling_high"] = round(rolling_high, 8)
            out["momentum_threshold"] = float(mom_thresh)
            out["volume"] = round(volume, 4)
            out["avg_volume"] = round(avg_volume, 4) if avg_volume == avg_volume else 0.0
            out["volume_multiplier"] = float(vol_mult)
            out["atr_pct"] = round(atr_pct, 8)
            out["atr_min"] = float(atr_min)
            out["atr_max"] = float(atr_max)
            if not (price_breakout and volume_confirms and volatility_ok):
                if not price_breakout:
                    out["near_miss_primary"] = "price_breakout"
                elif not volume_confirms:
                    out["near_miss_primary"] = "volume_confirms"
                elif not volatility_ok:
                    if atr_pct < float(atr_min):
                        out["near_miss_primary"] = "atr_below_min"
                    elif atr_pct > float(atr_max):
                        out["near_miss_primary"] = "atr_above_max"
                    else:
                        out["near_miss_primary"] = "volatility"
                out["reason_detail"] = "triple_gate: breakout + volume + ATR band"
        except Exception as exc:  # noqa: BLE001
            out["near_miss_primary"] = "diagnostic_error"
            out["reason_detail"] = str(exc)[:500]
        return out

    def _evaluate(self, symbol: str, df: pd.DataFrame) -> Optional[RawSignal]:

        cfg = self.effective_config()
        lookback   = cfg.get("lookback_periods", 20)
        vol_mult   = cfg.get("volume_multiplier", 1.5)
        atr_min    = cfg.get("atr_min", 0.005)
        atr_max    = cfg.get("atr_max", 0.05)
        mom_thresh = cfg.get("momentum_threshold", 0.002)

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

        sizing_md = self._compute_target_notional(confidence=float(confidence), atr_pct=float(atr_pct))

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
                **sizing_md,
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
