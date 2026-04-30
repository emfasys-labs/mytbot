"""
Volume/flow continuation + exhaustion strategy.

Captures short-horizon demand changes via:
- volume anomaly (z-score)
- bar return direction
- local trend confirmation
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Optional

import pandas as pd

from signals.engine import RawSignal
from strategies.base import Strategy


class VolumeFlowStrategy(Strategy):
    name = "volume_flow"

    def _compute_target_notional(self, *, confidence: float, flow_strength: float) -> dict[str, str]:
        cfg = self.effective_config()
        try:
            base_notional = Decimal(str(cfg.get("base_target_notional", "4000")))
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

    def no_setup_snapshot(self, symbol: str, features: pd.DataFrame) -> dict[str, Any]:
        """When no RawSignal, emit volume / return / z diagnostics for strategy_candidate_log."""
        out: dict[str, Any] = {"near_miss_kind": "volume_flow"}
        if not self.enabled or features is None or features.empty:
            out["near_miss_primary"] = "no_data"
            return out
        if "close" not in features.columns or "volume" not in features.columns:
            out["near_miss_primary"] = "missing_ohlcv"
            return out
        n = len(features)
        out["rows_available"] = n
        cfg = self.effective_config()
        lookback = int(cfg.get("volume_lookback", 20))
        z_open = float(cfg.get("zscore_open_threshold", 1.8))
        z_exhaust = float(cfg.get("zscore_exhaust_threshold", 3.4))
        min_ret = float(cfg.get("min_bar_return", 0.0015))
        out["zscore_open_threshold"] = z_open
        out["zscore_exhaust_threshold"] = z_exhaust
        out["min_bar_return"] = min_ret
        if n < 25:
            out["near_miss_primary"] = "insufficient_rows"
            return out
        latest = features.iloc[-1]
        prev = features.iloc[:-1]
        if len(prev) < lookback:
            out["near_miss_primary"] = "insufficient_rows"
            return out
        try:
            vol_hist = prev["volume"].tail(lookback)
            std_v = float(vol_hist.std())
            mean_v = float(vol_hist.mean())
            if std_v <= 0:
                std_v = max(mean_v * 0.05, 1.0)
            latest_close = float(latest["close"])
            prev_close = float(prev.iloc[-1]["close"])
            if prev_close <= 0:
                out["near_miss_primary"] = "invalid_price"
                return out
            bar_ret = (latest_close - prev_close) / prev_close
            z = (float(latest["volume"]) - mean_v) / std_v
            out["volume_z"] = round(z, 4)
            out["bar_return_abs"] = round(abs(float(bar_ret)), 8)
            ema_fast = float(features["close"].ewm(span=8, adjust=False).mean().iloc[-1])
            ema_slow = float(features["close"].ewm(span=21, adjust=False).mean().iloc[-1])
            trend_up = ema_fast > ema_slow
            trend_dn = ema_fast < ema_slow
            if ema_fast > ema_slow:
                ema_align = "up"
            elif ema_fast < ema_slow:
                ema_align = "down"
            else:
                ema_align = "flat"
            out["ema_alignment"] = ema_align
            exhaust = bool(z >= z_exhaust and abs(float(bar_ret)) < min_ret * 0.8)
            out["exhaustion_condition"] = exhaust
            cont_ok = z >= z_open and abs(float(bar_ret)) >= min_ret
            if not cont_ok and not exhaust and z < z_open:
                out["near_miss_primary"] = "low_volume_z"
            elif not cont_ok and not exhaust and abs(float(bar_ret)) < min_ret:
                out["near_miss_primary"] = "bar_return_too_small"
            elif cont_ok and not ((
                (float(bar_ret) > 0 and (trend_up or float(bar_ret) >= min_ret * 2.0))
                or (float(bar_ret) < 0 and (trend_dn or abs(float(bar_ret)) >= min_ret * 2.0))
            )):
                out["near_miss_primary"] = "trend_or_continuation"
            elif z >= z_exhaust and not exhaust:
                out["near_miss_primary"] = "exhaustion_not_faded"
            else:
                out["near_miss_primary"] = "not_triggered"
            out["reason_detail"] = "open vs continuation + exhaustion"
        except Exception as exc:  # noqa: BLE001
            out["near_miss_primary"] = "diagnostic_error"
            out["reason_detail"] = str(exc)[:500]
        return out

    def generate_signal(self, symbol: str, features: pd.DataFrame) -> Optional[RawSignal]:
        if not self.enabled or features is None or features.empty or len(features) < 25:
            return None
        if "close" not in features.columns or "volume" not in features.columns:
            return None

        cfg = self.effective_config()
        lookback = int(cfg.get("volume_lookback", 20))
        z_open = float(cfg.get("zscore_open_threshold", 1.8))
        z_exhaust = float(cfg.get("zscore_exhaust_threshold", 3.4))
        min_ret = float(cfg.get("min_bar_return", 0.0015))

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
