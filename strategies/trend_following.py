"""
strategies/trend_following.py
=============================
Time-series momentum via moving-average trend — the "shotgun" weapon (D158).

Smoother and more frequent than the breakout sniper: it holds a directional
view whenever a fast moving average is on one side of a slow one AND price
confirms, capturing the *body* of a trend rather than the breakout edge. This
is the single most robustly-documented systematic anomaly (time-series
momentum / trend), and it works precisely because trends persist longer than
random — so the average winning move clears costs.

Long when fast MA > slow MA and price > slow MA (established uptrend); short
on the mirror. Direction-agnostic. Medium horizon (days), so it is a
"trader/shotgun" temperament, not a knife.

Config (config/strategies.yaml::strategies.trend_following):
    enabled               bool
    fast_period           int    fast MA (e.g. 20)
    slow_period           int    slow MA (e.g. 50)
    min_separation_pct    float  fast/slow must differ by this fraction
                                 (avoid whipsaw when the MAs are entangled)
    base_target_notional  Decimal-as-string sizing intent
"""

from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

import pandas as pd

from signals.engine import RawSignal
from strategies.base import Strategy

logger = logging.getLogger(__name__)


class TrendFollowingStrategy(Strategy):
    name = "trend_following"

    def _params(self) -> Optional[dict[str, Any]]:
        cfg = self.effective_config()
        try:
            fast = int(cfg.get("fast_period", 20))
            slow = int(cfg.get("slow_period", 50))
            if fast >= slow:
                logger.debug("trend_following: fast_period must be < slow_period")
                return None
            return {
                "fast": fast,
                "slow": slow,
                "min_sep": float(cfg.get("min_separation_pct", 0.0)),
                "base_notional": cfg.get("base_target_notional", "20000"),
            }
        except (TypeError, ValueError):
            return None

    def generate_signal(self, symbol: str, features: pd.DataFrame) -> Optional[RawSignal]:
        if not self.enabled or features is None or features.empty:
            return None
        p = self._params()
        if p is None:
            return None
        if len(features) < p["slow"] + 1:
            return None
        try:
            close = features["close"]
            fast_ma = float(close.rolling(p["fast"]).mean().iloc[-1])
            slow_ma = float(close.rolling(p["slow"]).mean().iloc[-1])
            price = float(close.iloc[-1])
            if not (fast_ma == fast_ma and slow_ma == slow_ma) or slow_ma <= 0:
                return None
            sep = abs(fast_ma - slow_ma) / slow_ma
            if sep < p["min_sep"]:
                return None

            up = fast_ma > slow_ma and price > slow_ma
            dn = fast_ma < slow_ma and price < slow_ma
            if not (up or dn):
                return None

            side = "buy" if up else "sell"
            # Confidence grows with trend separation (steeper trend = stronger).
            confidence = min(0.55 + sep * 6.0, 0.90)

            target = self._target_notional(p["base_notional"], confidence)
            if target is None:
                return None
            return RawSignal(
                strategy=self.name,
                symbol=symbol,
                side=side,
                confidence=float(confidence),
                broker=self.preferred_broker,
                asset_class=self.asset_class,
                metadata={
                    "weapon_class": "shotgun",
                    "fast_ma": fast_ma,
                    "slow_ma": slow_ma,
                    "price": price,
                    "ma_separation_pct": sep,
                    "target_notional": target,
                },
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("trend_following error on %s: %s", symbol, exc)
            return None

    @staticmethod
    def _target_notional(base_raw: Any, confidence: float) -> Optional[str]:
        try:
            base = Decimal(str(base_raw))
        except (InvalidOperation, TypeError, ValueError):
            return None
        if base <= 0:
            return None
        conf = max(0.0, min(1.0, confidence))
        conf_scale = Decimal(str(0.75 + 0.5 * conf))
        target = max(base * Decimal("0.5"), min(base * Decimal("1.5"), base * conf_scale))
        return str(target.quantize(Decimal("0.01")))
