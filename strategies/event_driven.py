"""
Event-driven strategy powered by AI/news shock scores.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Optional

from signals.engine import RawSignal


class EventDrivenNewsStrategy:
    name = "event_driven_news"
    preferred_broker = "ibkr"

    def __init__(self, config: dict[str, Any]):
        self.config = dict(config or {})
        self.enabled = bool(self.config.get("enabled", False))

    def _compute_target_notional(self, confidence: float, shock: float) -> dict[str, str]:
        try:
            base_notional = Decimal(str(self.config.get("base_target_notional", "6000")))
        except (InvalidOperation, TypeError, ValueError):
            base_notional = Decimal("6000")
        if base_notional <= 0:
            base_notional = Decimal("6000")
        conf_scale = Decimal(str(max(0.85, min(1.35, 0.80 + confidence * 0.65))))
        shock_scale = Decimal(str(max(0.85, min(1.60, 1.0 + abs(shock) * 0.60))))
        target = (base_notional * conf_scale * shock_scale).quantize(Decimal("0.01"))
        return {
            "target_notional": str(target),
            "sizing_base_notional": str(base_notional.quantize(Decimal("0.01"))),
            "sizing_confidence_scale": str(conf_scale.quantize(Decimal("0.0001"))),
            "sizing_event_shock_scale": str(shock_scale.quantize(Decimal("0.0001"))),
            "sizing_intent_source": "event_shock_confidence",
        }

    def generate_from_context(
        self,
        *,
        symbol: str,
        asset_class: str,
        news_score: Optional[float],
        news_detail: Optional[dict[str, Any]],
        macro_regime: Optional[str],
        macro_confidence: Optional[float],
    ) -> Optional[RawSignal]:
        if not self.enabled or news_score is None:
            return None

        shock_threshold = float(self.config.get("shock_threshold", 0.45))
        score = float(news_score)
        if abs(score) < shock_threshold:
            return None

        side = "buy" if score > 0 else "sell"
        macro_boost = 0.04 if (macro_confidence is not None and float(macro_confidence) >= 0.65) else 0.0
        confidence = min(0.55 + (abs(score) - shock_threshold) * 0.65 + macro_boost, 0.96)
        md = self._compute_target_notional(confidence=confidence, shock=score)

        detail = dict(news_detail or {})
        return RawSignal(
            strategy=self.name,
            symbol=symbol,
            side=side,
            confidence=float(confidence),
            broker=self.preferred_broker,
            asset_class=asset_class,
            metadata={
                "event_type": str(detail.get("topic") or detail.get("reasoning") or "news_shock"),
                "news_score_hint": score,
                "ai_macro_regime": macro_regime or "unknown",
                "ai_macro_confidence": float(macro_confidence or 0.0),
                **md,
            },
        )
