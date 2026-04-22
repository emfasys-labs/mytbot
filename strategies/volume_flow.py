"""
Volume/flow continuation + exhaustion strategy.

Captures short-horizon demand changes via:
- volume anomaly (z-score)
- bar return direction
- local trend confirmation
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Optional

import pandas as pd

from signals.engine import RawSignal
from strategies.base import Strategy


class VolumeFlowStrategy(Strategy):
    name = "volume_flow"

    def _compute_target_notional(self, *, confidence: float, flow_strength: float) -> dict[str, str]:
        try:
            base_notional = Decimal(str(self.config.get("base_target_notional", "4000")))
        except (InvalidOperation, TypeError, ValueError):
            base_notional = Decimal("4000")
        if base_notional <= 0:
            base_notional = Decimal("4000")

        conf = max(0.0, min(1.0, float(confidence)))
        conf_scale = Decimal(str(0.80 + 0.50 * conf))
        flow_scale = Decimal(str(max(0.75, min(1.35, 1.0 + flow_strength * 0.25))))
        target = (base_notional * conf_scale * flow_scale).quantize(Decimal("0.01"))
        return {
            "target_notional": str(target),
            "sizing_base_notional": str(base_notional.quantize(Decimal("0.01"))),
            "sizing_confidence_scale": str(conf_scale.quantize(Decimal("0.0001"))),
            "sizing_flow_scale": str(flow_scale.quantize(Decimal("0.0001"))),
            "sizing_intent_source": "volume_flow_confidence",
        }

    def generate_signal(self, symbol: str, features: pd.DataFrame) -> Optional[RawSignal]:
        if not self.enabled or features is None or features.empty or len(features) < 25:
            return None
        if "close" not in features.columns or "volume" not in features.columns:
            return None

        lookback = int(self.config.get("volume_lookback", 20))
        z_open = float(self.config.get("zscore_open_threshold", 1.8))
        z_exhaust = float(self.config.get("zscore_exhaust_threshold", 3.4))
        min_ret = float(self.config.get("min_bar_return", 0.0015))

        latest = features.iloc[-1]
        prev = features.iloc[:-1]
        if len(prev) < lookback:
            return None

        vol_hist = prev["volume"].tail(lookback)
        std_v = float(vol_hist.std())
        mean_v = float(vol_hist.mean())
        if std_v <= 0:
            # Flat baseline volume still allows obvious demand shocks.
            std_v = max(mean_v * 0.05, 1.0)

        latest_close = float(latest["close"])
        prev_close = float(prev.iloc[-1]["close"])
        if prev_close <= 0:
            return None
        bar_ret = (latest_close - prev_close) / prev_close
        z = (float(latest["volume"]) - mean_v) / std_v

        ema_fast = float(features["close"].ewm(span=8, adjust=False).mean().iloc[-1])
        ema_slow = float(features["close"].ewm(span=21, adjust=False).mean().iloc[-1])
        trend_up = ema_fast > ema_slow
        trend_dn = ema_fast < ema_slow

        # Continuation regime: strong flow aligned with direction.
        if z >= z_open and abs(bar_ret) >= min_ret:
            if bar_ret > 0 and (trend_up or bar_ret >= min_ret * 2.0):
                confidence = min(0.58 + (z - z_open) * 0.07 + abs(bar_ret) * 7.0, 0.94)
                md = self._compute_target_notional(confidence=confidence, flow_strength=z)
                return RawSignal(
                    strategy=self.name,
                    symbol=symbol,
                    side="buy",
                    confidence=float(confidence),
                    broker=self.preferred_broker,
                    asset_class=self.asset_class,
                    metadata={
                        "flow_mode": "continuation",
                        "volume_z_score": round(z, 4),
                        "bar_return": round(bar_ret, 6),
                        **md,
                    },
                )
            if bar_ret < 0 and (trend_dn or abs(bar_ret) >= min_ret * 2.0):
                confidence = min(0.58 + (z - z_open) * 0.07 + abs(bar_ret) * 7.0, 0.94)
                md = self._compute_target_notional(confidence=confidence, flow_strength=z)
                return RawSignal(
                    strategy=self.name,
                    symbol=symbol,
                    side="sell",
                    confidence=float(confidence),
                    broker=self.preferred_broker,
                    asset_class=self.asset_class,
                    metadata={
                        "flow_mode": "continuation",
                        "volume_z_score": round(z, 4),
                        "bar_return": round(bar_ret, 6),
                        **md,
                    },
                )

        # Exhaustion reversal: very large spike + fading direction.
        if z >= z_exhaust and abs(bar_ret) < min_ret * 0.8:
            side = "sell" if trend_up else "buy"
            confidence = min(0.55 + (z - z_exhaust) * 0.05, 0.85)
            md = self._compute_target_notional(confidence=confidence, flow_strength=z * 0.75)
            return RawSignal(
                strategy=self.name,
                symbol=symbol,
                side=side,
                confidence=float(confidence),
                broker=self.preferred_broker,
                asset_class=self.asset_class,
                metadata={
                    "flow_mode": "exhaustion_reversal",
                    "volume_z_score": round(z, 4),
                    "bar_return": round(bar_ret, 6),
                    **md,
                },
            )
        return None
