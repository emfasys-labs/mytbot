"""
Lightweight meta-label filtering over candidate/raw strategy outputs.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import math
from typing import Any, Iterable

from core.models_runtime import SignalCandidate
from signals.engine import RawSignal


@dataclass
class MetaLabelResult:
    kept: int
    dropped: int
    avg_probability: float = 0.0


def _cfgf(cfg: dict[str, Any], key: str, default: float) -> float:
    try:
        return float(cfg.get(key, default))
    except (TypeError, ValueError):
        return default


def _effective_cfg(cfg: dict[str, Any], mode: str | None) -> dict[str, Any]:
    out = dict(cfg or {})
    if not mode:
        return out
    mc = out.get("mode_calibration", {}) or {}
    m = mc.get(str(mode).strip().lower(), {}) if isinstance(mc, dict) else {}
    if isinstance(m, dict):
        out.update(m)
    return out


def _sigmoid(x: float) -> float:
    x = max(-20.0, min(20.0, float(x)))
    return 1.0 / (1.0 + math.exp(-x))


def _strategy_bias(strategy_name: str, cfg: dict[str, Any]) -> float:
    priors = cfg.get("strategy_bias", {}) or {}
    try:
        return float(priors.get(str(strategy_name), 0.0))
    except (TypeError, ValueError):
        return 0.0


def _ml_probability(
    *,
    confidence: float,
    side: str,
    strategy_name: str,
    demand_score: float,
    metadata: dict[str, Any],
    cfg: dict[str, Any],
) -> float:
    conf = max(0.0, min(1.0, float(confidence)))
    side_sign = 1.0 if str(side).strip().lower() in {"buy", "long"} else -1.0
    align = max(-1.0, min(1.0, float(demand_score) * side_sign))
    try:
        vol_z = abs(float(metadata.get("volume_z_score", 0.0) or 0.0))
    except (TypeError, ValueError):
        vol_z = 0.0
    vol_feature = max(0.0, min(1.0, vol_z / 3.0))
    try:
        news_abs = abs(float(metadata.get("ai_news_score", metadata.get("news_score_hint", 0.0)) or 0.0))
    except (TypeError, ValueError):
        news_abs = 0.0
    news_feature = max(0.0, min(1.0, news_abs))
    try:
        demand_conf = float(metadata.get("demand_confidence", 0.0) or 0.0)
    except (TypeError, ValueError):
        demand_conf = 0.0
    demand_conf = max(0.0, min(1.0, demand_conf))

    w = cfg.get("ml_weights", {}) or {}
    lin = 0.0
    lin += float(w.get("bias", -0.15))
    lin += float(w.get("confidence", 1.80)) * conf
    lin += float(w.get("demand_alignment", 1.30)) * align
    lin += float(w.get("volume_feature", 0.45)) * vol_feature
    lin += float(w.get("news_feature", 0.55)) * news_feature
    lin += float(w.get("demand_confidence", 0.60)) * demand_conf
    lin += float(w.get("strategy_bias", 1.0)) * _strategy_bias(strategy_name, cfg)
    return _sigmoid(lin)


def keep_raw_signal(
    raw: RawSignal,
    *,
    demand_score: float,
    cfg: dict[str, Any],
    mode: str | None = None,
) -> bool:
    cfg_eff = _effective_cfg(cfg, mode)
    p = _ml_probability(
        confidence=float(raw.confidence),
        side=str(raw.side),
        strategy_name=str(raw.strategy),
        demand_score=float(demand_score),
        metadata=dict(raw.metadata or {}),
        cfg=cfg_eff,
    )
    prob_thr = _cfgf(cfg_eff, "probability_threshold", 0.54)
    return p >= prob_thr


def filter_candidates(
    candidates: Iterable[SignalCandidate],
    *,
    demand_score: float,
    cfg: dict[str, Any],
    mode: str | None = None,
) -> tuple[list[SignalCandidate], MetaLabelResult]:
    cfg_eff = _effective_cfg(cfg, mode)
    out: list[SignalCandidate] = []
    dropped = 0
    prob_thr = _cfgf(cfg_eff, "probability_threshold", 0.54)
    probs: list[float] = []
    for c in candidates:
        p = _ml_probability(
            confidence=float(c.confidence),
            side=str(c.side),
            strategy_name=str(c.strategy_name),
            demand_score=float(demand_score),
            metadata=dict(c.metadata or {}),
            cfg=cfg_eff,
        )
        probs.append(p)
        if p < prob_thr:
            dropped += 1
            continue
        out.append(c)
    avg_p = float(sum(probs) / len(probs)) if probs else 0.0
    return out, MetaLabelResult(kept=len(out), dropped=dropped, avg_probability=avg_p)
