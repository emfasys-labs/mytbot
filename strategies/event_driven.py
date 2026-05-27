"""
Event-driven strategy powered by AI/news shock scores.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Optional

from signals.engine import RawSignal
from system.dynamic_thresholds import (
    base_target_notional as dyn_base_notional,
    event_shock_threshold,
)
from system.adaptive_regime_weights import compute_multiplier as compute_regime_multiplier


class EventDrivenNewsStrategy:
    name = "event_driven_news"
    preferred_broker = "ibkr"

    def __init__(self, config: dict[str, Any]):
        self.config = dict(config or {})
        self.enabled = bool(self.config.get("enabled", False))

    def effective_config(self) -> dict[str, Any]:
        cfg = dict(self.config or {})
        mode = str(cfg.get("_active_profile_mode") or "").strip().lower()
        mode_cfg = cfg.get("mode_calibration", {}) or {}
        if mode and isinstance(mode_cfg, dict):
            override = mode_cfg.get(mode)
            if isinstance(override, dict):
                cfg.update(override)
        return cfg

    def _compute_target_notional(self, confidence: float, shock: float) -> dict[str, str]:
        # D141 — base notional live from NAV + P&L + regime.
        cfg = self.effective_config()
        try:
            static_base = Decimal(str(cfg.get("base_target_notional", "6000")))
        except (InvalidOperation, TypeError, ValueError):
            static_base = Decimal("6000")
        if static_base <= 0:
            static_base = Decimal("6000")
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
        conf_scale = Decimal(str(max(0.85, min(1.35, 0.80 + confidence * 0.65))))
        shock_scale = Decimal(str(max(0.85, min(1.60, 1.0 + abs(shock) * 0.60))))
        target = (base_notional * conf_scale * shock_scale).quantize(Decimal("0.01"))
        return {
            "target_notional": str(target),
            "sizing_base_notional": str(base_notional.quantize(Decimal("0.01"))),
            "sizing_confidence_scale": str(conf_scale.quantize(Decimal("0.0001"))),
            "sizing_event_shock_scale": str(shock_scale.quantize(Decimal("0.0001"))),
            "sizing_regime_mult": str(regime_mult),
            "sizing_intent_source": "event_shock_confidence_dyn",
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

        cfg = self.effective_config()
        # D141 — shock threshold computed live from the news-score
        # dispersion the caller passes through ``news_detail`` (or the
        # rolling dispersion stamped on signal pipeline output). When
        # the dispersion is unknown the formula falls back to the
        # caller's static value.
        score = float(news_score)
        detail = dict(news_detail or {})
        dispersion_hint = detail.get("news_score_dispersion")
        shock_threshold = float(event_shock_threshold(
            news_score_dispersion=dispersion_hint,
            static_threshold=cfg.get("shock_threshold", 0.45),
        ))
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
                "shock_threshold_dyn": shock_threshold,
                **md,
            },
        )
