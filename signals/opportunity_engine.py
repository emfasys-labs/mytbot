"""
signals/opportunity_engine.py
=============================
D015 opportunity scoring: full component blend + dynamic profile/regime weights.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Iterable, cast

from config.loaders import load_profile_modes
from config.models import AllocationConfig, ProfileModesConfig
from core.models_runtime import (
    AssetClass,
    Opportunity,
    OpportunityComponents,
    ProfileMode,
    RegimeState,
    Side,
    SignalCandidate,
    clip_decimal,
)
from data.feature_lookup import load_latest_features_for_symbols
from signals.d015_weights import volume_anomaly_weight_for_mode
from signals.opportunity_components import (
    score_liquidity_component,
    score_momentum_component,
    score_news_component,
    score_regime_alignment,
    score_relative_strength,
    score_structure_component,
)
from signals.volume_anomaly import (
    build_volume_anomaly_features_from_sources,
    detect_volume_flow,
    score_volume_anomaly_component,
)
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


def _map_asset_class(s: str) -> AssetClass:
    allowed: tuple[AssetClass, ...] = (
        "equity",
        "etf",
        "bond",
        "forex",
        "crypto",
        "future",
        "option",
        "other",
    )
    if s in allowed:
        return cast(AssetClass, s)
    return "other"


def _map_side(side: str) -> Side:
    x = (side or "").lower()
    if x in ("buy", "long"):
        return "long"
    if x in ("sell", "short"):
        return "short"
    return "long"


def _f(x) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def build_opportunities(
    *,
    signals: Iterable[SignalCandidate],
    regime_state: RegimeState,
    allocation_cfg: AllocationConfig,
    profile_cfg: ProfileModesConfig | None = None,
    active_profile_mode: ProfileMode | None = None,
    feature_json_by_symbol: dict[str, dict] | None = None,
    now: datetime | None = None,
) -> list[Opportunity]:
    """
    Full weighted opportunity score from ``allocation.yaml`` components.
    Volume weight is scaled by profile mode + regime (``profile_modes.yaml``).
    """
    ts = now or datetime.now(timezone.utc)
    sigs = list(signals)
    fj_map = feature_json_by_symbol or {}
    oe = allocation_cfg.opportunity_engine
    profile_cfg = profile_cfg or load_profile_modes()

    mom_vals: list[float] = []
    for c in sigs:
        fj = fj_map.get(c.symbol) or fj_map.get(c.symbol.upper()) or {}
        mom_vals.append(_f(fj.get("mom_10")))

    out: list[Opportunity] = []
    n = len(sigs)
    for idx, c in enumerate(sigs):
        one = Decimal("1")
        conf = clip_decimal(c.confidence, Decimal("0"), one)
        fj = fj_map.get(c.symbol) or fj_map.get(c.symbol.upper()) or {}

        mom_c = score_momentum_component(fj, oe.components.momentum)
        rank_frac = (
            sum(1 for m in mom_vals if m <= mom_vals[idx]) / float(n)
            if n
            else 0.5
        )
        rs_c = score_relative_strength(rank_frac, oe.components.relative_strength)

        vol_features = build_volume_anomaly_features_from_sources(
            feature_json=fj,
            signal_metadata=c.metadata,
        )
        vol_detection = detect_volume_flow(c.symbol, vol_features)
        base_vol_w = Decimal(str(oe.components.volume_anomaly.weight)) if oe.components.volume_anomaly.enabled else Decimal("0")
        mode_eff: ProfileMode = active_profile_mode or profile_cfg.defaults.active_mode
        if mode_eff not in profile_cfg.modes:
            mode_eff = profile_cfg.defaults.active_mode
        dyn_vol = volume_anomaly_weight_for_mode(profile_cfg, mode_eff, regime_state)
        vol_w = base_vol_w * dyn_vol if base_vol_w > 0 else Decimal("0")
        vol_score = score_volume_anomaly_component(vol_features, oe.components.volume_anomaly)

        news_c = score_news_component(c.metadata, oe.components.news_impact)
        reg_c = score_regime_alignment(mom_c, regime_state)
        liq_c = score_liquidity_component(c.metadata, oe.components.liquidity_quality)
        struct_c = score_structure_component(fj, oe.components.structure_quality)

        w_m = Decimal(str(oe.components.momentum.weight)) if oe.components.momentum.enabled else Decimal("0")
        w_v = vol_w
        w_n = Decimal(str(oe.components.news_impact.weight)) if oe.components.news_impact.enabled else Decimal("0")
        w_r = Decimal(str(oe.components.regime_alignment.weight)) if oe.components.regime_alignment.enabled else Decimal("0")
        w_l = Decimal(str(oe.components.liquidity_quality.weight)) if oe.components.liquidity_quality.enabled else Decimal("0")
        w_s = Decimal(str(oe.components.structure_quality.weight)) if oe.components.structure_quality.enabled else Decimal("0")
        w_rs = Decimal(str(oe.components.relative_strength.weight)) if oe.components.relative_strength.enabled else Decimal("0")

        numer = (
            w_m * mom_c
            + w_v * vol_score
            + w_n * news_c
            + w_r * reg_c
            + w_l * liq_c
            + w_s * struct_c
            + w_rs * rs_c
        )
        denom = w_m + w_v + w_n + w_r + w_l + w_s + w_rs
        opportunity_score = clip_decimal(numer / denom if denom > 0 else conf, Decimal("0"), one)
        demand_score = _f(regime_state.metadata.get("demand_score") if isinstance(regime_state.metadata, dict) else 0.0)
        side_sign = 1.0 if _map_side(c.side) == "long" else -1.0
        demand_alignment = max(-1.0, min(1.0, demand_score * side_sign))
        demand_mult = Decimal(str(max(0.85, min(1.15, 1.0 + demand_alignment * 0.10))))
        opportunity_score = clip_decimal(opportunity_score * demand_mult, Decimal("0"), one)

        ucfg = oe.scoring.urgency
        urgency = clip_decimal(
            Decimal(str(ucfg.base))
            + vol_detection.detection_strength * Decimal(str(ucfg.volume_detection))
            + conf * Decimal(str(ucfg.confidence)),
            Decimal("0"),
            one,
        )
        thr = Decimal(str(oe.scoring.volume_escalation_strength_threshold))
        esc = vol_detection.detection_strength >= thr
        if esc:
            urgency = clip_decimal(
                urgency * Decimal(str(ucfg.escalation_multiplier)),
                Decimal("0"),
                one,
            )

        comp = OpportunityComponents(
            momentum=mom_c,
            volume_anomaly=vol_score,
            news_impact=news_c,
            regime_alignment=reg_c,
            liquidity_quality=liq_c,
            structure_quality=struct_c,
            relative_strength=rs_c,
        )
        opp = Opportunity(
            symbol=c.symbol,
            asset_class=_map_asset_class(c.asset_class),
            side=_map_side(c.side),
            timestamp=ts,
            opportunity_score=opportunity_score,
            urgency_score=urgency,
            confidence=conf,
            components=comp,
            volume_flow=vol_detection,
            tags=[c.strategy_name],
            metadata={
                "strategy": c.strategy_name,
                "volume_refresh_context": vol_detection.refresh_context_recommended,
                "d015_escalate_context": bool(esc),
                "profile_mode": mode_eff,
                "demand_score": round(demand_score, 6),
                "demand_alignment": round(demand_alignment, 6),
            },
        )
        out.append(opp)

    if out:
        logger.debug(
            "opportunity_engine | count=%s allocator_enabled=%s",
            len(out),
            allocation_cfg.allocator.enabled,
        )
    return out


async def build_opportunities_async(
    *,
    signals: Iterable[SignalCandidate],
    regime_state: RegimeState,
    allocation_cfg: AllocationConfig,
    session: AsyncSession,
    timeframe: str = "1h",
    profile_cfg: ProfileModesConfig | None = None,
    active_profile_mode: ProfileMode | None = None,
    feature_json_by_symbol: dict[str, dict] | None = None,
    now: datetime | None = None,
) -> list[Opportunity]:
    sigs = list(signals)
    merged: dict[str, dict] = dict(feature_json_by_symbol or {})
    if sigs:
        syms = list({s.symbol for s in sigs})
        from_db = await load_latest_features_for_symbols(session, syms, timeframe)
        for k, v in from_db.items():
            merged.setdefault(k, v)
    return build_opportunities(
        signals=sigs,
        regime_state=regime_state,
        allocation_cfg=allocation_cfg,
        profile_cfg=profile_cfg,
        active_profile_mode=active_profile_mode,
        feature_json_by_symbol=merged,
        now=now,
    )
