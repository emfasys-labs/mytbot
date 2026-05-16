"""
signals/fusion.py — the AI evidence-fusion spine (Phase A).

This module defines the *uniform contract* every evidence producer (technical
components, forecast bridge, accumulator, news/macro AI, demand engine, regime
weighting, the meta-labeller, future sequence/regime/microstructure models)
emits, and a transparent combiner that turns those into a single
``FusionEvidence`` view.

Phase A scope — DELIBERATELY INERT ON LIVE DECISIONS:
  * It only *reads* values already computed and stamped onto a signal's
    ``metadata`` by the live pipeline (forecast_*, accumulator_*,
    ai_news_score, demand_*, regime_*, meta_label_*, volume_z_score). It
    never recomputes a model, never calls the meta-labeller, never mutates
    the signal, never vetoes/keeps anything.
  * The combiner weights are intentionally simple and *transparent*
    (confidence × a flat reliability prior). They are NOT learned and NOT
    calibrated yet — regime-conditional, calibrated, learned weights are a
    later phase. Phase A exists to lay the typed spine + shadow-compare the
    fused view against the live decision, nothing more.

Everything here is pure/synchronous and exception-safe by construction so it
can be called from a shadow path without risk.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from typing import Any

# ── The uniform contract ────────────────────────────────────────────────


@dataclass(slots=True)
class ModelSignal:
    """One evidence producer's calibrated-ish opinion, uniform shape.

    Every current and future model adapts to THIS so fusion is mechanical,
    not bespoke. ``fallback=True`` means the producer was absent/degraded
    (heuristic or missing inputs) and must carry ~no weight.
    """

    model_id: str
    model_version: str
    direction: float          # signed conviction, clamped [-1, +1]
    expected_return_bps: float  # horizon-scaled expected edge (0 if unknown)
    confidence: float         # [0, 1] reliability of THIS call
    horizon: str              # "intraday" | "swing" | "multi_day" | "n/a"
    regime_tag: str           # regime the producer believes we're in
    reliability_prior: float  # [0, 1] historical trust in current regime
    fallback: bool            # degraded/absent ⇒ weight ≈ 0

    def __post_init__(self) -> None:
        self.direction = _clamp(self.direction, -1.0, 1.0)
        self.confidence = _clamp(self.confidence, 0.0, 1.0)
        self.reliability_prior = _clamp(self.reliability_prior, 0.0, 1.0)
        if not math.isfinite(self.expected_return_bps):
            self.expected_return_bps = 0.0


@dataclass(slots=True)
class FusionInputs:
    symbol: str
    side: str                 # "long"/"short" (or "buy"/"sell")
    regime_label: str
    signals: list[ModelSignal] = field(default_factory=list)
    context: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class FusionEvidence:
    """Combined view — Phase A: for shadow logging / inspection ONLY."""

    symbol: str
    side: str
    regime_label: str
    combined_direction: float        # [-1, 1] consensus signed conviction
    combined_expected_edge_bps: float
    aggregate_confidence: float      # [0, 1]
    agreement: float                 # [0, 1] fraction agreeing on sign
    dispersion: float                # stdev of directions (0 = unanimous)
    meta_label_probability: float | None  # read-through, NOT recomputed
    n_models: int
    n_fallback: int
    contributing: list[str]          # model_ids that carried weight
    notes: str = ""


# ── helpers ─────────────────────────────────────────────────────────────


def _clamp(x: float, lo: float, hi: float) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return lo
    if not math.isfinite(v):
        return lo
    return max(lo, min(hi, v))


def _f(md: dict[str, Any], key: str, default: float = 0.0) -> float:
    try:
        v = md.get(key)
        if v is None or v == "":
            return default
        f = float(v)
        return f if math.isfinite(f) else default
    except (TypeError, ValueError):
        return default


def _sign(x: float) -> float:
    return 1.0 if x > 1e-12 else (-1.0 if x < -1e-12 else 0.0)


# Phase-A flat reliability prior. Uncalibrated on purpose — learned,
# regime-conditional priors replace this in a later phase. Kept as a named
# constant so the placeholder is obvious and easy to grep/replace.
_PHASE_A_RELIABILITY_PRIOR = 0.5


# ── adapter: existing live-computed metadata → ModelSignal[] ─────────────


def build_fusion_inputs_from_metadata(
    *,
    symbol: str,
    side: str,
    base_confidence: float,
    metadata: dict[str, Any],
) -> FusionInputs:
    """Read-only: turn values the LIVE pipeline already stamped on a signal
    into the uniform contract. Never recomputes a model. Missing/degraded
    sources become ``fallback=True`` neutral signals so the spine is robust.
    """
    md = metadata or {}
    regime = str(md.get("regime_label") or md.get("regime") or "unknown")
    sigs: list[ModelSignal] = []

    # 1. Price forecast bridge (the future sequence-model slot).
    fc_used = bool(md.get("forecast_used"))
    fc_ret = _f(md, "forecast_expected_return", 0.0)  # fractional return
    fc_conf = _f(md, "forecast_confidence_blended", _f(md, "forecast_confidence", 0.0))
    sigs.append(ModelSignal(
        model_id="price_forecast",
        model_version=str(md.get("forecast_model_version", "bridge")),
        direction=_sign(fc_ret) * min(1.0, abs(fc_ret) * 50.0),
        expected_return_bps=fc_ret * 10_000.0,
        confidence=fc_conf if fc_used else 0.0,
        horizon="swing",
        regime_tag=regime,
        reliability_prior=_PHASE_A_RELIABILITY_PRIOR,
        fallback=not fc_used,
    ))

    # 2. Stateful conviction accumulator.
    has_acc = "accumulator_score" in md
    acc = _f(md, "accumulator_score", 0.0)
    sigs.append(ModelSignal(
        model_id="accumulator",
        model_version="v1",
        direction=_clamp(acc, -1.0, 1.0),
        expected_return_bps=0.0,
        confidence=_f(md, "accumulator_confidence", 0.0),
        horizon="swing",
        regime_tag=regime,
        reliability_prior=_PHASE_A_RELIABILITY_PRIOR,
        fallback=not has_acc,
    ))

    # 3. News / macro AI.
    has_news = ("ai_news_score" in md) or ("news_score" in md)
    news = _f(md, "ai_news_score", _f(md, "news_score", 0.0))
    sigs.append(ModelSignal(
        model_id="news_ai",
        model_version="ai_pipeline",
        direction=_clamp(news, -1.0, 1.0),
        expected_return_bps=0.0,
        confidence=min(1.0, abs(news)),
        horizon="intraday",
        regime_tag=regime,
        reliability_prior=_PHASE_A_RELIABILITY_PRIOR,
        fallback=not has_news,
    ))

    # 4. Demand engine (cross-asset/flow regime alignment).
    has_dem = "demand_score" in md
    dem_align = _f(md, "demand_alignment", 0.0)
    sigs.append(ModelSignal(
        model_id="demand_engine",
        model_version="v2",
        direction=_clamp(dem_align, -1.0, 1.0),
        expected_return_bps=0.0,
        confidence=_clamp(abs(_f(md, "demand_score", 0.0)), 0.0, 1.0),
        horizon="swing",
        regime_tag=regime,
        reliability_prior=_PHASE_A_RELIABILITY_PRIOR,
        fallback=not has_dem,
    ))

    # 5. Regime-strategy weighting (context, ~direction-neutral).
    has_rw = "regime_strategy_multiplier" in md
    rw = _f(md, "regime_strategy_multiplier", 1.0)
    sigs.append(ModelSignal(
        model_id="regime_weight",
        model_version="phase4",
        direction=0.0,
        expected_return_bps=0.0,
        confidence=_clamp(abs(rw - 1.0) / 0.5, 0.0, 1.0),
        horizon="n/a",
        regime_tag=regime,
        reliability_prior=_PHASE_A_RELIABILITY_PRIOR,
        fallback=not has_rw,
    ))

    # 6. Meta-labeller — READ-THROUGH of the value the live path already
    #    computed (we never invoke the labeller here).
    has_meta = "meta_label_probability" in md
    mlp = _f(md, "meta_label_probability", 0.5)
    sigs.append(ModelSignal(
        model_id="meta_labeler",
        model_version=str(md.get("meta_label_model_version", "live")),
        direction=0.0,  # it judges trust, not direction
        expected_return_bps=0.0,
        confidence=_clamp(mlp, 0.0, 1.0),
        horizon="n/a",
        regime_tag=regime,
        reliability_prior=_PHASE_A_RELIABILITY_PRIOR,
        fallback=not has_meta,
    ))

    # 7. Volume anomaly (technical evidence already computed).
    has_vz = "volume_z_score" in md
    vz = _f(md, "volume_z_score", 0.0)
    sigs.append(ModelSignal(
        model_id="volume_anomaly",
        model_version="v1",
        direction=0.0,
        expected_return_bps=0.0,
        confidence=_clamp(abs(vz) / 4.0, 0.0, 1.0),
        horizon="intraday",
        regime_tag=regime,
        reliability_prior=_PHASE_A_RELIABILITY_PRIOR,
        fallback=not has_vz,
    ))

    return FusionInputs(
        symbol=symbol,
        side=side,
        regime_label=regime,
        signals=sigs,
        context={"base_confidence": _clamp(base_confidence, 0.0, 1.0)},
    )


# ── the combiner (transparent, NOT learned — Phase A) ───────────────────


def fuse(fi: FusionInputs) -> FusionEvidence:
    """Combine ModelSignals into one evidence view.

    Phase A combiner is deliberately simple and explainable: weight =
    confidence × reliability_prior, fallback signals excluded. This is an
    inspection/shadow artefact only — it does not size or gate anything.
    Learned, regime-conditional, calibrated weights come later.
    """
    active = [s for s in fi.signals if not s.fallback and s.confidence > 0.0]
    n_models = len(fi.signals)
    n_fallback = sum(1 for s in fi.signals if s.fallback)

    if not active:
        return FusionEvidence(
            symbol=fi.symbol, side=fi.side, regime_label=fi.regime_label,
            combined_direction=0.0, combined_expected_edge_bps=0.0,
            aggregate_confidence=0.0, agreement=0.0, dispersion=0.0,
            meta_label_probability=_meta_prob(fi),
            n_models=n_models, n_fallback=n_fallback,
            contributing=[], notes="no_active_evidence",
        )

    weights = [max(1e-9, s.confidence * s.reliability_prior) for s in active]
    wsum = sum(weights)
    cdir = sum(w * s.direction for w, s in zip(weights, active)) / wsum
    cedge = sum(w * s.expected_return_bps for w, s in zip(weights, active)) / wsum
    aconf = sum(w * s.confidence for w, s in zip(weights, active)) / wsum

    # Agreement: among signals that express a direction, fraction whose
    # sign matches the combined direction.
    directional = [s for s in active if abs(s.direction) > 1e-9]
    if directional and abs(cdir) > 1e-9:
        agree = sum(1 for s in directional if _sign(s.direction) == _sign(cdir))
        agreement = agree / len(directional)
        dispersion = (
            statistics.pstdev([s.direction for s in directional])
            if len(directional) > 1 else 0.0
        )
    else:
        agreement = 0.0
        dispersion = 0.0

    return FusionEvidence(
        symbol=fi.symbol, side=fi.side, regime_label=fi.regime_label,
        combined_direction=_clamp(cdir, -1.0, 1.0),
        combined_expected_edge_bps=cedge,
        aggregate_confidence=_clamp(aconf, 0.0, 1.0),
        agreement=_clamp(agreement, 0.0, 1.0),
        dispersion=dispersion,
        meta_label_probability=_meta_prob(fi),
        n_models=n_models, n_fallback=n_fallback,
        contributing=[s.model_id for s in active],
        notes="phase_a_transparent_combiner",
    )


def _meta_prob(fi: FusionInputs) -> float | None:
    for s in fi.signals:
        if s.model_id == "meta_labeler" and not s.fallback:
            return s.confidence
    return None
