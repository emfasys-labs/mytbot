"""
Global demand composite signal.

Produces a bounded score in [-1, 1] using:
- AI news polarity (cross-symbol mean)
- AI macro regime/confidence tilt
- Cross-asset anchor returns (risk-on vs defensive)
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from functools import lru_cache
from pathlib import Path
from typing import Any

import pandas as pd

from system.cross_asset_demand_graph import CrossAssetDemandGraph
from system.relational_demand_graph import RelationalDemandGraphArtifact, evaluate_relational_shadow

@dataclass
class DemandSignal:
    score: float
    trend: str
    confidence: float
    components: dict[str, float]


class DemandEngine:
    def __init__(self, config: dict[str, Any] | None = None):
        self.config = dict(config or {})
        self.enabled = bool(self.config.get("enabled", True))
        self._prev_score = 0.0
        self._graph = CrossAssetDemandGraph(self.config)

    @staticmethod
    def _clip(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
        return max(lo, min(hi, float(x)))

    @staticmethod
    def _safe_mean(vals: list[float]) -> float:
        if not vals:
            return 0.0
        return float(sum(vals) / len(vals))

    def _ai_component(self, ai_result: Any) -> tuple[float, float]:
        if ai_result is None:
            return 0.0, 0.0
        news_scores = getattr(ai_result, "news_scores", {}) or {}
        arr: list[float] = []
        for v in news_scores.values():
            if v is None:
                continue
            try:
                arr.append(float(v))
            except (TypeError, ValueError):
                continue
        news_mean = self._safe_mean(arr)
        macro_regime = str(getattr(ai_result, "macro_regime", "") or "").strip().lower()
        macro_conf = float(getattr(ai_result, "macro_confidence", 0.0) or 0.0)
        macro_tilt = 0.0
        if macro_regime in {"risk_on", "trend_up"}:
            macro_tilt = 0.5 * macro_conf
        elif macro_regime in {"risk_off", "trend_down", "crash", "panic"}:
            macro_tilt = -0.5 * macro_conf
        return self._clip(news_mean), self._clip(macro_tilt)

    def _cross_asset_component(self, feature_map: dict[str, pd.DataFrame]) -> float:
        g = self._graph.evaluate(feature_map)
        return self._clip(g.score)

    def compute(self, *, ai_result: Any, feature_map: dict[str, pd.DataFrame]) -> DemandSignal:
        if not self.enabled:
            return DemandSignal(score=0.0, trend="flat", confidence=0.0, components={})

        ai_news, ai_macro = self._ai_component(ai_result)
        graph = self._graph.evaluate(feature_map)
        cross_asset = self._clip(graph.score)
        learned_shadow = self._learned_graph_shadow(feature_map)

        w_news = float(self.config.get("weights", {}).get("news", 0.35))
        w_macro = float(self.config.get("weights", {}).get("macro", 0.30))
        w_cross = float(self.config.get("weights", {}).get("cross_asset", 0.35))
        total_w = max(0.001, abs(w_news) + abs(w_macro) + abs(w_cross))
        raw = (w_news * ai_news + w_macro * ai_macro + w_cross * cross_asset) / total_w
        raw = self._clip(raw)

        # Lightweight EMA smoothing to avoid whipsaw strategy gating.
        alpha = float(self.config.get("smoothing_alpha", 0.35))
        alpha = max(0.05, min(0.95, alpha))
        score = self._clip((1.0 - alpha) * self._prev_score + alpha * raw)
        self._prev_score = score

        trend_threshold = float(self.config.get("trend_threshold", 0.20))
        if score >= trend_threshold:
            trend = "rising"
        elif score <= -trend_threshold:
            trend = "falling"
        else:
            trend = "flat"
        confidence = min(1.0, abs(score) * 1.3)
        components = {
            "ai_news": ai_news,
            "ai_macro": ai_macro,
            "cross_asset": cross_asset,
            "market_volatility": float(graph.market_volatility),
            "cross_asset_coverage": float(graph.coverage),
        }
        components.update(learned_shadow)
        return DemandSignal(
            score=score,
            trend=trend,
            confidence=confidence,
            components=components,
        )

    def _learned_graph_shadow(self, feature_map: dict[str, pd.DataFrame]) -> dict[str, float | int | bool]:
        cfg = self.config.get("learned_graph_shadow") or {}
        if not bool(cfg.get("enabled", False)):
            return {"learned_cross_asset_shadow_used": False}
        path = str(cfg.get("artifact_path") or "").strip()
        if not path:
            return {"learned_cross_asset_shadow_used": False}
        artifact = _load_learned_graph_artifact(path)
        if artifact is None:
            return {"learned_cross_asset_shadow_used": False}
        return evaluate_relational_shadow(
            artifact,
            feature_map,
            scale=float(cfg.get("scale", self.config.get("cross_asset_scale", 30.0))),
        )


@lru_cache(maxsize=4)
def _load_learned_graph_artifact(path: str) -> RelationalDemandGraphArtifact | None:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        return RelationalDemandGraphArtifact.from_dict(raw)
    except Exception:  # noqa: BLE001
        return None
