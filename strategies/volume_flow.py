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
from system.dynamic_thresholds import (
    base_target_notional as dyn_base_notional,
    min_bar_return_threshold,
    volume_zscore_exhaust_threshold,
    volume_zscore_open_threshold,
)
from system.adaptive_regime_weights import compute_multiplier as compute_regime_multiplier


class VolumeFlowStrategy(Strategy):
    name = "volume_flow"

    def _compute_target_notional(self, *, confidence: float, flow_strength: float) -> dict[str, str]:
        # D141 — base notional resolved live from NAV + strategy P&L
        # health + regime multiplier. Confidence + flow_strength
        # scaling still applies on top.
        cfg = self.effective_config()
        try:
            static_base = Decimal(str(cfg.get("base_target_notional", "4000")))
        except (InvalidOperation, TypeError, ValueError):
            static_base = Decimal("4000")
        if static_base <= 0:
            static_base = Decimal("4000")
        live_features = cfg.get("_regime_features") or {}
        regime_mult = compute_regime_multiplier(self.name, live_features)
        dyn_base = dyn_base_notional(
            nav=cfg.get("_nav") or 0,
            strategy_net_pnl_recent=cfg.get("_strategy_pnl_recent") or 0,
            strategy_total_fills_recent=cfg.get("_strategy_fills_recent") or 0,
            regime_multiplier=regime_mult,
            quarantine_multiplier=cfg.get("_strategy_quarantine_mult") or 1,
            static_notional=static_base,
        )
        base_notional = dyn_base if dyn_base > 0 else static_base

        conf = max(0.0, min(1.0, float(confidence)))
        conf_scale = Decimal(str(0.80 + 0.50 * conf))
        flow_scale = Decimal(str(max(0.75, min(1.35, 1.0 + flow_strength * 0.25))))
        target = (base_notional * conf_scale * flow_scale).quantize(Decimal("0.01"))
        return {
            "target_notional": str(target),
            "sizing_base_notional": str(base_notional.quantize(Decimal("0.01"))),
            "sizing_confidence_scale": str(conf_scale.quantize(Decimal("0.0001"))),
            "sizing_flow_scale": str(flow_scale.quantize(Decimal("0.0001"))),
            "sizing_regime_mult": str(regime_mult),
            "sizing_quarantine_mult": str(cfg.get("_strategy_quarantine_mult") or "1"),
            "strategy_quarantine_state": str(cfg.get("_strategy_quarantine_state") or "normal"),
            "sizing_intent_source": "volume_flow_confidence_dyn",
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
        # D141 — diagnostics quote the LIVE thresholds (formula output),
        # so the candidate log shows what the gate actually compared.
        prev_for_atr = features.iloc[:-1]
        try:
            atr_close = float(prev_for_atr["close"].iloc[-1])
            atr_range = float((prev_for_atr["high"] - prev_for_atr["low"]).tail(lookback).mean())
            atr_pct_diag = atr_range / atr_close if atr_close > 0 else 0.0
        except Exception:  # noqa: BLE001
            atr_pct_diag = 0.0
        z_open = float(volume_zscore_open_threshold(
            atr_pct=atr_pct_diag,
            static_threshold=cfg.get("zscore_open_threshold", 1.8),
        ))
        z_exhaust = float(volume_zscore_exhaust_threshold(
            atr_pct=atr_pct_diag,
            static_threshold=cfg.get("zscore_exhaust_threshold", 3.4),
        ))
        min_ret = float(min_bar_return_threshold(
            atr_pct=atr_pct_diag,
            static_threshold=cfg.get("min_bar_return", 0.0015),
        ))
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
        static_z_open = float(cfg.get("zscore_open_threshold", 1.8))
        static_z_exhaust = float(cfg.get("zscore_exhaust_threshold", 3.4))
        static_min_ret = float(cfg.get("min_bar_return", 0.0015))

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

        # D141 — replace static thresholds with live-formula values.
        # ATR drives both the z-score thresholds (wild market → require
        # bigger spike) and the min-bar-return floor.
        try:
            atr_close = float(prev["close"].iloc[-1])
            atr_range = float((prev["high"] - prev["low"]).tail(lookback).mean())
            atr_pct_now = atr_range / atr_close if atr_close > 0 else 0.0
        except Exception:  # noqa: BLE001
            atr_pct_now = 0.0
        z_open = float(volume_zscore_open_threshold(
            atr_pct=atr_pct_now,
            static_threshold=static_z_open,
        ))
        z_exhaust = float(volume_zscore_exhaust_threshold(
            atr_pct=atr_pct_now,
            static_threshold=static_z_exhaust,
        ))
        min_ret = float(min_bar_return_threshold(
            atr_pct=atr_pct_now,
            static_threshold=static_min_ret,
        ))

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
