"""
Macro demand regime rotation strategy.

Trades configured proxies based on global demand score/trend:
- risk-on symbols prefer long when demand rises
- risk-off symbols prefer long when demand falls
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Optional

from signals.engine import RawSignal


class RegimeRotationStrategy:
    name = "regime_rotation"
    preferred_broker = "ibkr"

    def __init__(self, config: dict[str, Any] | None = None):
        self.config = dict(config or {})
        self.enabled = bool(self.config.get("enabled", False))

    def _compute_target_notional(self, confidence: float) -> dict[str, str]:
        try:
            base_notional = Decimal(str(self.config.get("base_target_notional", "5500")))
        except (InvalidOperation, TypeError, ValueError):
            base_notional = Decimal("5500")
        if base_notional <= 0:
            base_notional = Decimal("5500")
        conf_scale = Decimal(str(max(0.8, min(1.4, 0.75 + confidence * 0.70))))
        target = (base_notional * conf_scale).quantize(Decimal("0.01"))
        return {
            "target_notional": str(target),
            "sizing_base_notional": str(base_notional.quantize(Decimal("0.01"))),
            "sizing_confidence_scale": str(conf_scale.quantize(Decimal("0.0001"))),
            "sizing_intent_source": "regime_rotation",
        }

    def generate_from_demand(
        self,
        *,
        symbol: str,
        asset_class: str,
        demand_score: float,
        demand_trend: str,
        demand_confidence: float,
    ) -> Optional[RawSignal]:
        if not self.enabled:
            return None

        s = symbol.strip().upper()
        risk_on = {x.strip().upper() for x in self.config.get("risk_on_symbols", ["SPY", "QQQ", "XLE", "BTC-USD"])}
        risk_off = {x.strip().upper() for x in self.config.get("risk_off_symbols", ["TLT", "GLD", "UUP"])}
        trigger = float(self.config.get("score_trigger", 0.35))
        if abs(demand_score) < trigger:
            return None

        side: str | None = None
        regime_bucket: str | None = None
        if s in risk_on:
            side = "buy" if demand_score > 0 else "sell"
            regime_bucket = "risk_on_proxy"
        elif s in risk_off:
            side = "buy" if demand_score < 0 else "sell"
            regime_bucket = "risk_off_proxy"
        if side is None:
            return None

        confidence = min(0.56 + abs(demand_score) * 0.35 + demand_confidence * 0.20, 0.94)
        md = self._compute_target_notional(confidence)
        return RawSignal(
            strategy=self.name,
            symbol=symbol,
            side=side,
            confidence=float(confidence),
            broker=self.preferred_broker,
            asset_class=asset_class,
            metadata={
                "demand_score": round(float(demand_score), 6),
                "demand_trend": demand_trend,
                "demand_confidence": round(float(demand_confidence), 6),
                "regime_bucket": regime_bucket,
                **md,
            },
        )
