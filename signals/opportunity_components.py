"""Per-component scores for D015 opportunity engine (bounded Decimals)."""

from __future__ import annotations

import math
from decimal import Decimal
from typing import Any, Mapping

from config.models import (
    LiquidityQualityComponentConfig,
    MomentumComponentConfig,
    NewsImpactComponentConfig,
    WeightedComponentConfig,
)
from core.models_runtime import RegimeState, clip_decimal
from core.signal_math import tanh_clip


def _d(x: float) -> Decimal:
    return Decimal(str(x))


def _f(x: Any) -> float:
    if x is None:
        return 0.0
    try:
        v = float(x)
        if math.isnan(v):
            return 0.0
        return v
    except (TypeError, ValueError):
        return 0.0


def score_momentum_component(fj: Mapping[str, Any], cfg: MomentumComponentConfig) -> Decimal:
    if not cfg.enabled:
        return Decimal("0")
    sc = cfg.subcomponents
    mom = _f(fj.get("mom_10"))
    mom_n = tanh_clip(Decimal(str(mom / max(1e-9, 15.0))))
    rsi = _f(fj.get("rsi_14"))
    rsi_n = (
        clip_decimal(_d((rsi - 30.0) / 40.0), Decimal("0"), Decimal("1")) if rsi else Decimal("0.5")
    )
    bb_p = _f(fj.get("BBP_20_2.0"))
    if bb_p == 0 and "BBP_20_2" in fj:
        bb_p = _f(fj.get("BBP_20_2"))
    bb_n = clip_decimal(_d(bb_p), Decimal("0"), Decimal("1")) if bb_p else Decimal("0.5")
    gv = _f(fj.get("garch_vol_1d"))
    atr_n = clip_decimal(_d(min(1.0, gv * 12.0)), Decimal("0"), Decimal("1")) if gv > 0 else Decimal("0.3")
    trend_p = mom_n
    raw = (
        _d(sc.z_return_5m) * mom_n
        + _d(sc.z_return_1h) * mom_n
        + _d(sc.breakout_strength) * bb_n
        + _d(sc.trend_slope) * atr_n
        + _d(sc.trend_persistence) * trend_p
    )
    return clip_decimal(tanh_clip(raw), Decimal("0"), Decimal("1"))


def score_news_component(md: Mapping[str, Any], cfg: NewsImpactComponentConfig) -> Decimal:
    if not cfg.enabled:
        return Decimal("0")
    fw = cfg.formula
    ns = md.get("news_score")
    if ns is None:
        return Decimal("0.5")
    s = clip_decimal(_d(float(ns)), Decimal("-1"), Decimal("1"))
    sent = (s + Decimal("1")) / Decimal("2")
    cred = _d(float(md.get("news_credibility", 1.0)))
    mat = _d(float(md.get("news_materiality", 1.0)))
    fresh = _d(float(md.get("news_freshness", 1.0)))
    out = sent * cred * mat * fresh
    wsum = _d(fw.sentiment + fw.credibility + fw.materiality + fw.freshness) / Decimal("4")
    return clip_decimal(out * wsum, Decimal("0"), Decimal("1"))


def score_regime_alignment(mom_score: Decimal, regime: RegimeState) -> Decimal:
    sign = Decimal("1") if mom_score >= Decimal("0.5") else Decimal("-1")
    ts = regime.components.trend_strength
    aligned = Decimal("1") - abs(ts - (Decimal("0.5") + sign * Decimal("0.25")))
    return clip_decimal(aligned, Decimal("0"), Decimal("1"))


def score_liquidity_component(md: Mapping[str, Any], cfg: LiquidityQualityComponentConfig) -> Decimal:
    if not cfg.enabled:
        return Decimal("0")
    sp_bps = _f(md.get("spread_bps", md.get("spread_bps_estimate", 25)))
    slip_bps = _f(md.get("slippage_bps_estimate", 15))
    depth = _f(md.get("depth_fragility", 0.3))
    p = cfg.penalties
    pen = (
        _d(p.spread) * _d(min(1.0, sp_bps / 100.0))
        + _d(p.slippage_estimate) * _d(min(1.0, slip_bps / 80.0))
        + _d(p.depth_fragility) * _d(depth)
    )
    return clip_decimal(Decimal("1") - pen, Decimal("0"), Decimal("1"))


def score_structure_component(fj: Mapping[str, Any], cfg: WeightedComponentConfig) -> Decimal:
    if not cfg.enabled:
        return Decimal("0")
    hurst = _f(fj.get("hurst_dfa_128"))
    garch = _f(fj.get("garch_vol_1d"))
    h_score = Decimal("0.5")
    if hurst > 0:
        h_score = clip_decimal(_d(1.0 - abs(hurst - 0.55) * 2.0), Decimal("0"), Decimal("1"))
    g_score = clip_decimal(Decimal("1") - tanh_clip(Decimal(str(garch * 25))), Decimal("0"), Decimal("1"))
    return clip_decimal((h_score + g_score) / Decimal("2"), Decimal("0"), Decimal("1"))


def score_relative_strength(rank_fraction: float, cfg: WeightedComponentConfig) -> Decimal:
    if not cfg.enabled:
        return Decimal("0")
    return clip_decimal(_d(rank_fraction), Decimal("0"), Decimal("1"))
