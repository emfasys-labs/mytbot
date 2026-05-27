"""
strategies/mean_reversion.py
============================
Simple RSI + Bollinger mean reversion strategy for M3.
"""

from __future__ import annotations

import logging
from typing import Optional
from decimal import Decimal, InvalidOperation

import pandas as pd

from signals.engine import RawSignal
from strategies.base import Strategy
from system.dynamic_thresholds import (
    base_target_notional as dyn_base_notional,
    bollinger_band_epsilon,
    rsi_thresholds,
)
from system.adaptive_regime_weights import compute_multiplier as compute_regime_multiplier

logger = logging.getLogger(__name__)


class MeanReversionStrategy(Strategy):
    """
    Buy when price is oversold and outside lower Bollinger Band.
    Sell when price is overbought and outside upper Bollinger Band.
    """

    name = "mean_reversion"

    def _compute_target_notional(self, *, confidence: float, atr_pct: float) -> dict[str, str]:
        """D141 — base notional is now derived live from NAV, recent
        per-strategy P&L health, and the live regime multiplier. The
        confidence + volatility scalings still apply on top. Static
        ``base_target_notional`` is used only as a fallback when the
        ``dynamic_thresholds`` YAML block is disabled or NAV is
        unavailable."""
        cfg = self.effective_config()
        try:
            static_base = Decimal(str(cfg.get("base_target_notional", "5000")))
        except (InvalidOperation, TypeError, ValueError):
            static_base = Decimal("5000")
        if static_base <= 0:
            static_base = Decimal("5000")

        # Pull live inputs that the trading loop pushes into config each
        # iteration. If they aren't there we degrade gracefully to the
        # static base (legacy behaviour).
        nav_raw = cfg.get("_nav") or 0
        pnl_raw = cfg.get("_strategy_pnl_recent") or 0
        fills_raw = cfg.get("_strategy_fills_recent") or 0
        live_features = cfg.get("_regime_features") or {}
        regime_mult = compute_regime_multiplier(self.name, live_features)
        dyn_base = dyn_base_notional(
            nav=nav_raw,
            strategy_net_pnl_recent=pnl_raw,
            strategy_total_fills_recent=fills_raw,
            regime_multiplier=regime_mult,
            static_notional=static_base,
        )
        base_notional = dyn_base if dyn_base > 0 else static_base

        conf = max(0.0, min(1.0, float(confidence)))
        conf_scale = Decimal(str(0.75 + 0.5 * conf))  # 0.75x .. 1.25x

        try:
            atr = max(float(atr_pct), 0.0)
        except (TypeError, ValueError):
            atr = 0.0
        if atr > 0:
            raw_vol_scale = 0.02 / atr
            vol_scale = Decimal(str(max(0.70, min(1.30, raw_vol_scale))))
        else:
            vol_scale = Decimal("1.0")

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

    def generate_signal(self, symbol: str, features: pd.DataFrame) -> Optional[RawSignal]:
        if not self.enabled or features is None or features.empty:
            return None
        try:
            return self._evaluate(symbol, features)
        except Exception as exc:  # noqa: BLE001
            logger.error("mean_reversion error on {}: {}", symbol, exc)
            return None

    def _evaluate(self, symbol: str, df: pd.DataFrame) -> Optional[RawSignal]:
        cfg = self.effective_config()
        lookback = int(cfg.get("lookback_periods", 30))
        if len(df) < lookback:
            return None

        latest = df.iloc[-1]
        close = float(latest["close"])
        rsi = self._to_float(latest.get("rsi_14"))
        bb_lower = self._to_float(self._get_latest_band_value(df, latest, "BBL"))
        bb_upper = self._to_float(self._get_latest_band_value(df, latest, "BBU"))
        if rsi is None or bb_lower is None or bb_upper is None:
            return None

        # D141 — compute thresholds LIVE from the symbol's own ATR and the
        # market-wide state score. Falls back to the legacy YAML literals
        # when dynamic_thresholds is disabled in YAML.
        atr_for_rsi = self._calculate_atr(df, lookback)
        atr_pct_now = (atr_for_rsi / close) if close > 0 else 0.0
        market_state_score = cfg.get("_market_state_score", 0)
        rsi_buy_d, rsi_sell_d = rsi_thresholds(
            atr_pct=atr_pct_now,
            market_state_score=market_state_score,
            static_buy_threshold=cfg.get("rsi_buy_threshold", 30.0),
            static_sell_threshold=cfg.get("rsi_sell_threshold", 70.0),
        )
        band_epsilon_d = bollinger_band_epsilon(
            atr_pct=atr_pct_now,
            static_epsilon=cfg.get("band_epsilon", 0.0),
        )
        rsi_buy = float(rsi_buy_d)
        rsi_sell = float(rsi_sell_d)
        band_epsilon = float(band_epsilon_d)

        buy_setup = rsi <= rsi_buy and close <= bb_lower * (1.0 + band_epsilon)
        sell_setup = rsi >= rsi_sell and close >= bb_upper * (1.0 - band_epsilon)
        if not buy_setup and not sell_setup:
            return None

        side = "buy" if buy_setup else "sell"
        center = (bb_lower + bb_upper) / 2.0
        stretch = abs(close - center) / center if center > 0 else 0.0
        confidence = min(0.55 + stretch * 5.0, 0.95)
        # ATR already computed above for the dynamic threshold step;
        # reuse it (avoid double-compute on every signal).
        atr_pct = atr_pct_now

        sizing_md = self._compute_target_notional(confidence=float(confidence), atr_pct=float(atr_pct))

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
                # D141 — record the live-computed thresholds so the
                # candidate log can show exactly which RSI / band-prox
                # values fired the setup at this market state.
                "rsi_buy_threshold_dyn": rsi_buy,
                "rsi_sell_threshold_dyn": rsi_sell,
                "band_epsilon_dyn": band_epsilon,
                "market_state_score_used": float(market_state_score or 0),
                **sizing_md,
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
