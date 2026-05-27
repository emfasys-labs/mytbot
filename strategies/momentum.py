"""
strategies/momentum.py
=======================
Momentum Breakout Strategy (M3, made symmetric 2026-05-26).

Logic:
- BUY when price breaks above N-period rolling high with confirming volume.
- SELL (short) when price breaks below N-period rolling low with confirming volume.
- Both sides require volatility (ATR%) inside the configured band.

Every parameter is YAML-driven from ``config/strategies.yaml``. The class has
**no hardcoded fallbacks** — if a key is missing the strategy refuses to
generate a signal rather than invent a constant.

Required config keys (config/strategies.yaml::strategies.momentum_breakout):
    enabled               bool
    lookback_periods      int    rolling high/low window
    volume_multiplier     float  volume must be N× the rolling average
    atr_min               float  ATR% lower bound (avoid dead markets)
    atr_max               float  ATR% upper bound (avoid chaos)
    momentum_threshold    float  min relative break beyond rolling high/low
    base_target_notional  Decimal-as-string for sizing intent
"""

from typing import Any, Optional
from decimal import Decimal, InvalidOperation
import pandas as pd
import logging

from strategies.base import Strategy
from signals.engine import RawSignal
from system.dynamic_thresholds import (
    acceptable_atr_band,
    base_target_notional as dyn_base_notional,
    momentum_breakout_threshold,
    volume_confirmation_multiplier,
)
from system.adaptive_regime_weights import compute_multiplier as compute_regime_multiplier

logger = logging.getLogger(__name__)


_REQUIRED_KEYS = (
    "lookback_periods",
    "volume_multiplier",
    "atr_min",
    "atr_max",
    "momentum_threshold",
)


class MomentumBreakoutStrategy(Strategy):
    """
    Momentum breakout on liquid assets, **bidirectional**.
    Suitable for: US large-cap equities, BTC, ETH, major ETFs.
    Timeframe: 5-minute to 1-hour candles.
    """

    name = "momentum_breakout"

    def _resolve_params(self) -> Optional[dict[str, Any]]:
        """Pull every required parameter from config. Return None if any
        required key is missing — never substitute a hardcoded default."""
        cfg = self.effective_config()
        missing = [k for k in _REQUIRED_KEYS if cfg.get(k) is None]
        if missing:
            logger.debug(
                "%s: missing required config keys %s — strategy idle",
                self.name, missing,
            )
            return None
        try:
            return {
                "lookback": int(cfg["lookback_periods"]),
                "vol_mult": float(cfg["volume_multiplier"]),
                "atr_min": float(cfg["atr_min"]),
                "atr_max": float(cfg["atr_max"]),
                "mom_thresh": float(cfg["momentum_threshold"]),
                "base_notional_raw": cfg.get("base_target_notional"),
            }
        except (TypeError, ValueError) as exc:
            logger.debug("%s: invalid config value (%s) — strategy idle", self.name, exc)
            return None

    def _compute_target_notional(self, *, confidence: float, atr_pct: float) -> Optional[dict[str, str]]:
        """D141 — base notional resolved live from NAV + strategy P&L
        health + regime multiplier. Confidence + volatility scaling
        still apply on top. The static YAML value is used as a
        structural fallback (no tuning invented in code).
        """
        cfg = self.effective_config()
        raw = cfg.get("base_target_notional")
        if raw is None:
            logger.debug("%s: base_target_notional missing — cannot size", self.name)
            return None
        try:
            static_base = Decimal(str(raw))
        except (InvalidOperation, TypeError, ValueError):
            logger.debug("%s: base_target_notional=%r invalid — cannot size", self.name, raw)
            return None
        if static_base <= 0:
            logger.debug("%s: base_target_notional=%s non-positive — cannot size", self.name, static_base)
            return None

        # Pull live inputs from the trading loop's per-iteration config push.
        live_features = cfg.get("_regime_features") or {}
        regime_mult = compute_regime_multiplier(self.name, live_features)
        dyn_base = dyn_base_notional(
            nav=cfg.get("_nav") or 0,
            strategy_net_pnl_recent=cfg.get("_strategy_pnl_recent") or 0,
            strategy_total_fills_recent=cfg.get("_strategy_fills_recent") or 0,
            regime_multiplier=regime_mult,
            static_notional=static_base,
        )
        base_notional = dyn_base if dyn_base > 0 else static_base

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
            "sizing_regime_mult": str(regime_mult),
            "sizing_intent_source": "strategy_confidence_volatility_dyn",
        }

    def generate_signal(
        self,
        symbol: str,
        features: pd.DataFrame,
    ) -> Optional[RawSignal]:
        """
        Bidirectional momentum-breakout signal generation.
        Returns None if no breakout (either side) is detected.
        """
        if not self.enabled:
            return None
        params = self._resolve_params()
        if params is None:
            return None
        if len(features) < params["lookback"] + 1:
            logger.debug("%s: not enough data (%d rows)", symbol, len(features))
            return None
        try:
            signal = self._evaluate(symbol, features, params)
            if signal:
                logger.info(
                    "SIGNAL %s | %s %s | confidence=%.2f",
                    self.name, symbol, signal.side, signal.confidence,
                )
            return signal
        except Exception as exc:  # noqa: BLE001
            logger.error("%s error on %s: %s", self.name, symbol, exc)
            return None

    def no_setup_snapshot(self, symbol: str, df: pd.DataFrame) -> dict[str, Any]:
        """When :meth:`generate_signal` would return None, log near-miss diagnostics (D043).

        Reports both bullish and bearish breakout gates so a single primary
        blocker can be identified per cycle (e.g. ``price_breakout_either_side``,
        ``volume_confirms``, ``atr_below_min``).
        """
        out: dict[str, Any] = {"near_miss_kind": "momentum_breakout"}
        if not self.enabled:
            out["near_miss_primary"] = "strategy_disabled"
            return out
        params = self._resolve_params()
        if params is None:
            out["near_miss_primary"] = "config_incomplete"
            return out
        lookback = params["lookback"]
        if df is None or len(df) < lookback + 1:
            out["near_miss_primary"] = "insufficient_rows"
            out["rows_available"] = 0 if df is None or df.empty else len(df)
            out["min_rows"] = lookback + 1
            return out
        try:
            latest = df.iloc[-1]
            prev = df.iloc[:-1]
            close = float(latest["close"])
            volume = float(latest["volume"])
            rolling_high = float(prev["high"].rolling(lookback).max().iloc[-1])
            rolling_low = float(prev["low"].rolling(lookback).min().iloc[-1])
            avg_volume = float(prev["volume"].rolling(lookback).mean().iloc[-1])
            atr = self._calculate_atr(df, lookback)
            atr_pct = float(atr / close) if close > 0 else 0.0
            # D141 — diagnostics use the live thresholds so the candidate
            # log shows the actual gate values the strategy compared
            # against, not stale YAML literals.
            median_atr_pct = float(prev["high"].sub(prev["low"]).div(prev["close"]).rolling(lookback).median().iloc[-1])
            vol_std = float(prev["volume"].tail(lookback).std()) if len(prev) >= lookback else 0.0
            vol_mean = float(prev["volume"].tail(lookback).mean()) if len(prev) >= lookback else 1.0
            vol_z = ((volume - vol_mean) / vol_std) if vol_std > 0 else 0.0
            atr_min_d, atr_max_d = acceptable_atr_band(
                median_atr_pct=median_atr_pct,
                static_min=params["atr_min"],
                static_max=params["atr_max"],
            )
            mom_d = momentum_breakout_threshold(
                atr_pct=atr_pct,
                static_threshold=params["mom_thresh"],
            )
            vol_mult_d = volume_confirmation_multiplier(
                volume_z_recent=vol_z,
                static_multiplier=params["vol_mult"],
            )
            mom = float(mom_d)
            vol_mult = float(vol_mult_d)
            atr_min = float(atr_min_d)
            atr_max = float(atr_max_d)
            break_up = close > rolling_high * (1 + mom)
            break_dn = close < rolling_low * (1 - mom) if rolling_low > 0 else False
            volume_confirms = volume > avg_volume * vol_mult
            volatility_ok = atr_min <= atr_pct <= atr_max
            out.update({
                "price_breakout_up": break_up,
                "price_breakout_down": break_dn,
                "volume_confirms": volume_confirms,
                "volatility_ok": volatility_ok,
                "close": round(close, 8),
                "rolling_high": round(rolling_high, 8),
                "rolling_low": round(rolling_low, 8),
                "momentum_threshold": mom,
                "volume": round(volume, 4),
                "avg_volume": round(avg_volume, 4) if avg_volume == avg_volume else 0.0,
                "volume_multiplier": vol_mult,
                "atr_pct": round(atr_pct, 8),
                "atr_min": atr_min,
                "atr_max": atr_max,
            })
            if not ((break_up or break_dn) and volume_confirms and volatility_ok):
                if not (break_up or break_dn):
                    out["near_miss_primary"] = "price_breakout_either_side"
                elif not volume_confirms:
                    out["near_miss_primary"] = "volume_confirms"
                elif not volatility_ok:
                    if atr_pct < atr_min:
                        out["near_miss_primary"] = "atr_below_min"
                    elif atr_pct > atr_max:
                        out["near_miss_primary"] = "atr_above_max"
                    else:
                        out["near_miss_primary"] = "volatility"
                out["reason_detail"] = "triple_gate: bidirectional breakout + volume + ATR band"
        except Exception as exc:  # noqa: BLE001
            out["near_miss_primary"] = "diagnostic_error"
            out["reason_detail"] = str(exc)[:500]
        return out

    def _evaluate(
        self,
        symbol: str,
        df: pd.DataFrame,
        params: dict[str, Any],
    ) -> Optional[RawSignal]:
        lookback = params["lookback"]
        cfg = self.effective_config()

        latest = df.iloc[-1]
        prev = df.iloc[:-1]

        close = float(latest["close"])
        volume = float(latest["volume"])
        rolling_high = float(prev["high"].rolling(lookback).max().iloc[-1])
        rolling_low = float(prev["low"].rolling(lookback).min().iloc[-1])
        avg_volume = float(prev["volume"].rolling(lookback).mean().iloc[-1])

        atr = self._calculate_atr(df, lookback)
        atr_pct = atr / close if close > 0 else 0.0

        # D141 — replace static thresholds with live formulas. ATR drives
        # the momentum threshold + acceptable band; recent volume z drives
        # the confirmation multiplier.
        median_atr_pct = float(prev["high"].sub(prev["low"]).div(prev["close"]).rolling(lookback).median().iloc[-1])
        vol_std = float(prev["volume"].tail(lookback).std()) if len(prev) >= lookback else 0.0
        vol_mean = float(prev["volume"].tail(lookback).mean()) if len(prev) >= lookback else 1.0
        vol_z = ((volume - vol_mean) / vol_std) if vol_std > 0 else 0.0

        atr_min_d, atr_max_d = acceptable_atr_band(
            median_atr_pct=median_atr_pct,
            static_min=params["atr_min"],
            static_max=params["atr_max"],
        )
        mom_d = momentum_breakout_threshold(
            atr_pct=atr_pct,
            static_threshold=params["mom_thresh"],
        )
        vol_mult_d = volume_confirmation_multiplier(
            volume_z_recent=vol_z,
            static_multiplier=params["vol_mult"],
        )
        atr_min = float(atr_min_d)
        atr_max = float(atr_max_d)
        mom = float(mom_d)
        vol_mult = float(vol_mult_d)

        # ── Breakout conditions (bidirectional) ──────────────────────────────
        break_up = close > rolling_high * (1 + mom)
        break_dn = close < rolling_low * (1 - mom) if rolling_low > 0 else False
        if not (break_up or break_dn):
            return None
        if not (atr_min <= atr_pct <= atr_max):
            return None
        if not (avg_volume > 0 and volume > avg_volume * vol_mult):
            return None

        # ── Direction + confidence ───────────────────────────────────────────
        if break_up:
            side = "buy"
            breakout_strength = (close - rolling_high) / rolling_high if rolling_high > 0 else 0.0
            ref_level = rolling_high
        else:
            side = "sell"
            breakout_strength = (rolling_low - close) / rolling_low if rolling_low > 0 else 0.0
            ref_level = rolling_low

        volume_strength = min((volume / avg_volume) / 3, 0.3)
        confidence = min(0.5 + breakout_strength * 10 + volume_strength, 0.95)

        sizing_md = self._compute_target_notional(
            confidence=float(confidence), atr_pct=float(atr_pct),
        )
        if sizing_md is None:
            # Config missing base_target_notional → don't fabricate a size.
            return None

        return RawSignal(
            strategy=self.name,
            symbol=symbol,
            side=side,
            confidence=float(confidence),
            broker=self.preferred_broker,
            asset_class=self.asset_class,
            metadata={
                "breakout_direction": "up" if break_up else "down",
                "rolling_high": float(rolling_high),
                "rolling_low": float(rolling_low),
                "ref_level": float(ref_level),
                "close": float(close),
                "breakout_strength": float(breakout_strength),
                "volume_ratio": float(volume / avg_volume) if avg_volume > 0 else 0.0,
                "atr_pct": float(atr_pct),
                "lookback": lookback,
                # D141 — forensic record of which live thresholds fired.
                "momentum_threshold_dyn": mom,
                "volume_multiplier_dyn": vol_mult,
                "atr_min_dyn": atr_min,
                "atr_max_dyn": atr_max,
                "median_atr_pct": median_atr_pct,
                "volume_z_recent": vol_z,
                **sizing_md,
            },
        )

    def _calculate_atr(self, df: pd.DataFrame, period: int) -> float:
        """Average True Range — measures volatility."""
        high = df["high"]
        low = df["low"]
        close = df["close"]
        prev_close = close.shift(1)
        tr = pd.concat([
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ], axis=1).max(axis=1)
        return float(tr.rolling(period).mean().iloc[-1])
