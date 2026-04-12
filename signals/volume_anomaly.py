"""
Volume / flow anomaly: structured features, detection, and component scoring (D015 phase 2).

Separation of concerns:
- **Features** — measurable inputs (may be partial).
- **Detection** — “something changed”; may recommend context refresh; still no trade.
- **Component score** — bounded scalar fed into `Opportunity.components.volume_anomaly` for the allocator.

Data sources today:
- M2 `feature_snapshots.features` JSON (`vol_ratio`, `vpin_proxy_50`, … from `data/features.py`).
- Strategy / loop metadata (e.g. `volume_z_score` from `system.trading_loop.helpers.enrich_signal_volume_z`).
- Optional metadata hints: `trade_count_z`, `volume_persistence_hint`, `orderbook_imbalance_bps`.

Venues without L2 or prints leave fields at zero; scoring degrades gracefully.
"""

from __future__ import annotations

import math
from decimal import Decimal
from typing import Any, Mapping

from config.models import VolumeAnomalyComponentConfig
from core.models_runtime import (
    VolumeAnomalyDetectionResult,
    VolumeAnomalyFeatures,
    clip_decimal,
)
from core.signal_math import bounded_sigmoid, normalize_zscore, tanh_clip

# Detection-only thresholds (anchors; can move to allocation.yaml later).
_REFRESH_VOLUME_Z = Decimal("2.25")
_REFRESH_REL_DOLLAR_VOL = Decimal("0.55")


def _d(x: Any) -> Decimal:
    if x is None:
        return Decimal("0")
    try:
        return Decimal(str(float(x)))
    except (TypeError, ValueError, ArithmeticError):
        return Decimal("0")


def build_volume_anomaly_features_from_sources(
    *,
    feature_json: Mapping[str, Any] | None = None,
    signal_metadata: Mapping[str, Any] | None = None,
) -> VolumeAnomalyFeatures:
    """
    Merge feature-store JSON and signal metadata into a single structured feature row.

    Prefers M2-persisted keys (``volume_z``, ``relative_dollar_volume``, …) when present;
    falls back to ``vol_ratio`` / metadata heuristics.
    """
    fj = dict(feature_json or {})
    md = dict(signal_metadata or {})

    volume_z = _d(md.get("volume_z_score"))
    if volume_z == 0 and fj.get("volume_z") is not None and not (
        isinstance(fj.get("volume_z"), float) and math.isnan(fj.get("volume_z"))
    ):
        volume_z = _d(fj.get("volume_z"))

    rel = Decimal("0")
    rdv = fj.get("relative_dollar_volume")
    if rdv is not None and not (isinstance(rdv, float) and math.isnan(rdv)):
        r = float(rdv)
        if r > 1.0:
            rel = clip_decimal(_d((r - 1.0) / 3.0), Decimal("0"), Decimal("1"))
    else:
        vr = fj.get("vol_ratio")
        if vr is not None and not (isinstance(vr, float) and math.isnan(vr)):
            v = float(vr)
            if v > 1.0:
                rel = clip_decimal(_d((v - 1.0) / 3.0), Decimal("0"), Decimal("1"))

    trade_raw = _d(md.get("trade_count_z"))
    if trade_raw == 0 and fj.get("trade_count_anomaly") is not None:
        trade_raw = _d(fj.get("trade_count_anomaly"))
    if trade_raw == 0:
        trade_raw = _d(md.get("trade_count_anomaly"))
    if trade_raw > 1 or trade_raw < Decimal("0"):
        trade_ct = normalize_zscore(clip_decimal(trade_raw, Decimal("-3"), Decimal("3")))
    else:
        trade_ct = clip_decimal(trade_raw, Decimal("0"), Decimal("1"))

    obi = _d(md.get("orderbook_imbalance_bps"))
    if obi == 0:
        vpin = fj.get("vpin_proxy_50")
        if vpin is not None and not (isinstance(vpin, float) and math.isnan(vpin)):
            obi = clip_decimal(_d(vpin), Decimal("0"), Decimal("1"))

    persistence = Decimal("0")
    if fj.get("volume_persistence") is not None:
        persistence = clip_decimal(_d(fj.get("volume_persistence")), Decimal("0"), Decimal("1"))
    if persistence == 0:
        persistence = clip_decimal(_d(md.get("volume_persistence_hint")), Decimal("0"), Decimal("1"))

    fake = Decimal("0")
    if fj.get("fake_spike_penalty") is not None and not (
        isinstance(fj.get("fake_spike_penalty"), float) and math.isnan(fj.get("fake_spike_penalty"))
    ):
        fake = clip_decimal(_d(fj.get("fake_spike_penalty")), Decimal("0"), Decimal("1"))
    if fake == 0:
        if rel > Decimal("0.65") and obi < Decimal("0.15") and volume_z > Decimal("1.5"):
            fake = Decimal("0.45")
        elif rel > Decimal("0.85") and obi < Decimal("0.12"):
            fake = Decimal("0.35")

    return VolumeAnomalyFeatures(
        volume_z=volume_z,
        relative_dollar_volume=rel,
        trade_count_anomaly=clip_decimal(trade_ct, Decimal("0"), Decimal("1")),
        orderbook_imbalance=clip_decimal(obi, Decimal("-1"), Decimal("1")),
        volume_persistence=persistence,
        fake_spike_penalty=clip_decimal(fake, Decimal("0"), Decimal("1")),
    )


def detect_volume_flow(
    symbol: str,
    features: VolumeAnomalyFeatures,
    *,
    volume_z_for_refresh: Decimal | None = None,
    rel_vol_for_refresh: Decimal | None = None,
) -> VolumeAnomalyDetectionResult:
    """
    Detection layer: classify “unusual activity” and whether to refresh news/depth/context.

    Does **not** open positions or change sizing by itself.
    """
    z_thr = volume_z_for_refresh if volume_z_for_refresh is not None else _REFRESH_VOLUME_Z
    r_thr = rel_vol_for_refresh if rel_vol_for_refresh is not None else _REFRESH_REL_DOLLAR_VOL

    z_unit = normalize_zscore(features.volume_z)
    refresh = features.volume_z >= z_thr or features.relative_dollar_volume >= r_thr

    strength_raw = (
        Decimal("0.35") * z_unit
        + Decimal("0.30") * features.relative_dollar_volume
        + Decimal("0.15") * features.trade_count_anomaly
        + Decimal("0.15") * abs(features.orderbook_imbalance)
        + Decimal("0.05") * features.volume_persistence
    )
    strength_raw = clip_decimal(strength_raw, Decimal("0"), Decimal("1"))
    penalty = features.fake_spike_penalty
    strength = clip_decimal(strength_raw * (Decimal("1") - Decimal("0.5") * penalty), Decimal("0"), Decimal("1"))

    return VolumeAnomalyDetectionResult(
        features=features,
        refresh_context_recommended=bool(refresh),
        detection_strength=strength,
        metadata={"symbol": symbol, "layer": "detection"},
    )


def score_volume_anomaly_component(
    features: VolumeAnomalyFeatures,
    comp_cfg: VolumeAnomalyComponentConfig,
) -> Decimal:
    """
    Reaction-side scalar for the opportunity stack (allocator input).

    Uses ``allocation.yaml`` sub-weights and saturating transform. Applies fake-spike
    dampening and optional persistence boost from config flags.
    """
    if not comp_cfg.enabled:
        return Decimal("0")

    sc = comp_cfg.subcomponents
    z_u = normalize_zscore(features.volume_z)
    obi = features.orderbook_imbalance
    if obi >= Decimal("0") and obi <= Decimal("1"):
        obi_u = obi
    else:
        obi_u = clip_decimal((obi + Decimal("1")) / Decimal("2"), Decimal("0"), Decimal("1"))

    weighted = (
        _d(sc.volume_z) * z_u
        + _d(sc.relative_dollar_volume) * features.relative_dollar_volume
        + _d(sc.trade_count_anomaly) * features.trade_count_anomaly
        + _d(sc.orderbook_imbalance) * obi_u
    )

    if comp_cfg.transforms.persistence_boost_enabled and features.volume_persistence > 0:
        weighted = weighted * (Decimal("1") + Decimal("0.25") * features.volume_persistence)

    if comp_cfg.transforms.fake_spike_penalty_enabled and features.fake_spike_penalty > 0:
        weighted = weighted * (Decimal("1") - Decimal("0.6") * features.fake_spike_penalty)

    fn = comp_cfg.transforms.saturating_function
    sat = tanh_clip(weighted) if fn == "tanh" else bounded_sigmoid(weighted)
    return clip_decimal(sat, Decimal("0"), Decimal("1"))
