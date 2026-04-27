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


def _apply_trained_meta_label_to_opportunity(
    opp: Opportunity,
    *,
    signal: SignalCandidate,
    regime_state: RegimeState,
    config,
) -> bool:
    """
    Wave 2 — score an opportunity through the trained meta-labeller.

    Attaches ``meta_label_*`` keys to ``opp.metadata`` regardless of
    outcome and returns ``True`` if the opportunity should be kept.
    Defensive — any exception is logged and the opportunity is kept
    (fail-open) so a labeller bug never silently disables D015.
    """
    from models.schemas import Mode
    from signals.trained_meta_labeler import evaluate_features

    side_sign = 1.0 if _map_side(signal.side) == "long" else -1.0
    features: dict[str, float] = {
        "strategy_confidence": _f(signal.confidence),
        "raw_confidence": _f(signal.raw_signal_strength),
        "side_sign": side_sign,
        "opportunity_score": _f(opp.opportunity_score),
        "urgency_score": _f(opp.urgency_score),
        "momentum": _f(opp.components.momentum),
        "volume_anomaly": _f(opp.components.volume_anomaly),
        "news_impact": _f(opp.components.news_impact),
        "regime_alignment": _f(opp.components.regime_alignment),
        "liquidity_quality": _f(opp.components.liquidity_quality),
        "structure_quality": _f(opp.components.structure_quality),
        "relative_strength": _f(opp.components.relative_strength),
    }
    for k, v in (signal.metadata or {}).items():
        if k in features:
            continue
        try:
            features[k] = float(v)
        except (TypeError, ValueError):
            continue

    regime_label = None
    try:
        regime_label = getattr(regime_state, "label", None)
        if regime_label is not None:
            regime_label = str(regime_label)
    except Exception:  # noqa: BLE001
        regime_label = None

    try:
        decision = evaluate_features(
            features=features,
            mode=Mode.PAPER,
            config=config,
            regime=regime_label,
            portfolio_mode=str(opp.metadata.get("profile_mode") or "") or None,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("opportunity_engine | meta-label eval failed: %s", exc)
        opp.metadata["meta_label_error"] = str(exc)
        return True

    opp.metadata["meta_label_probability"] = (
        None if decision.probability is None else float(decision.probability)
    )
    opp.metadata["meta_label_threshold"] = float(decision.threshold)
    opp.metadata["meta_label_reason"] = decision.reason
    opp.metadata["meta_label_kept"] = bool(decision.kept)
    if decision.model_name:
        opp.metadata["meta_label_model_name"] = decision.model_name
    if decision.model_version:
        opp.metadata["meta_label_model_version"] = decision.model_version
    if decision.feature_hash:
        opp.metadata["meta_label_feature_hash"] = decision.feature_hash
    return bool(decision.kept)


def build_opportunities(
    *,
    signals: Iterable[SignalCandidate],
    regime_state: RegimeState,
    allocation_cfg: AllocationConfig,
    profile_cfg: ProfileModesConfig | None = None,
    active_profile_mode: ProfileMode | None = None,
    feature_json_by_symbol: dict[str, dict] | None = None,
    now: datetime | None = None,
    trained_meta_labeler_config=None,
) -> list[Opportunity]:
    """
    Full weighted opportunity score from ``allocation.yaml`` components.
    Volume weight is scaled by profile mode + regime (``profile_modes.yaml``).

    When ``trained_meta_labeler_config`` is supplied (Wave 2), every built
    opportunity is scored through the trained meta-labeller. Decisions
    are attached to ``Opportunity.metadata`` (``meta_label_*`` keys) so
    the dashboard funnel can render them; opportunities the labeller
    rejects are dropped before they reach the allocator. The labeller
    itself enforces the registry/approval contract.
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
        if trained_meta_labeler_config is not None and getattr(
            trained_meta_labeler_config, "enabled", False
        ):
            keep = _apply_trained_meta_label_to_opportunity(
                opp,
                signal=c,
                regime_state=regime_state,
                config=trained_meta_labeler_config,
            )
            if not keep:
                logger.info(
                    "Opportunity SKIPPED meta_label | %s %s | reason=%s prob=%s thr=%s",
                    opp.symbol,
                    opp.side,
                    opp.metadata.get("meta_label_reason"),
                    opp.metadata.get("meta_label_probability"),
                    opp.metadata.get("meta_label_threshold"),
                )
                continue
        out.append(opp)

    if out:
        logger.debug(
            "opportunity_engine | count=%s allocator_enabled=%s",
            len(out),
            allocation_cfg.allocator.enabled,
        )
    return out


_FACTOR_SLEEVE_CFG_UNLOADED = object()
_FACTOR_SLEEVE_CFG_CACHE: object = _FACTOR_SLEEVE_CFG_UNLOADED


def _get_default_factor_sleeve_config():
    """Lazy loader for the default factor-sleeve YAML.

    Cached on first call so each loop iteration doesn't re-parse the
    file. ``None`` is returned if the YAML is absent or fails to parse;
    callers treat that as "disabled".
    """
    global _FACTOR_SLEEVE_CFG_CACHE
    if _FACTOR_SLEEVE_CFG_CACHE is not _FACTOR_SLEEVE_CFG_UNLOADED:
        return _FACTOR_SLEEVE_CFG_CACHE
    try:
        from strategies.factor_sleeve import FactorSleeveConfig

        _FACTOR_SLEEVE_CFG_CACHE = FactorSleeveConfig.load()
    except Exception as exc:  # noqa: BLE001
        logger.warning("opportunity_engine | factor_sleeve config load failed: %s", exc)
        _FACTOR_SLEEVE_CFG_CACHE = None
    return _FACTOR_SLEEVE_CFG_CACHE


def reset_factor_sleeve_cache() -> None:
    """Test helper — drops the cached FactorSleeveConfig."""
    global _FACTOR_SLEEVE_CFG_CACHE
    _FACTOR_SLEEVE_CFG_CACHE = _FACTOR_SLEEVE_CFG_UNLOADED


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
    trained_meta_labeler_config=None,
    factor_sleeve_config=None,
    factor_sleeve_universe: list[str] | None = None,
    factor_sleeve_lookback_bars: int = 300,
    factor_sleeve_asset_class_for_symbol: dict[str, str] | None = None,
    factor_sleeve_benchmark_symbol: str | None = None,
    factor_sleeve_fundamentals_for_symbol: dict[str, dict] | None = None,
) -> list[Opportunity]:
    sigs = list(signals)

    # Wave 3 wiring — when the factor sleeve is enabled, fetch its
    # cross-sectional candidates and merge them into the per-strategy
    # signal stream BEFORE opportunity scoring. The sleeve's universe
    # defaults to the symbols already in the signal batch (so it only
    # acts on symbols the rest of the pipeline is already evaluating);
    # callers can pass an explicit ``factor_sleeve_universe`` to widen
    # it. When the sleeve is disabled (the default) this is a no-op.
    sleeve_cfg = factor_sleeve_config
    if sleeve_cfg is None:
        sleeve_cfg = _get_default_factor_sleeve_config()
    if sleeve_cfg is not None and getattr(sleeve_cfg, "enabled", False):
        try:
            from strategies.factor_sleeve_runner import collect_factor_sleeve_candidates

            universe = factor_sleeve_universe or sorted({s.symbol for s in sigs})
            extra = await collect_factor_sleeve_candidates(
                session,
                universe,
                timeframe=timeframe,
                lookback_bars=factor_sleeve_lookback_bars,
                config=sleeve_cfg,
                asset_class_for_symbol=factor_sleeve_asset_class_for_symbol,
                benchmark_symbol=factor_sleeve_benchmark_symbol,
                fundamentals_for_symbol=factor_sleeve_fundamentals_for_symbol,
                as_of=now,
            )
            if extra:
                # Avoid duplicate (symbol, side, strategy) candidates if a
                # per-symbol strategy has already emitted one for the
                # same direction.
                seen = {(s.symbol, s.side, s.strategy_name) for s in sigs}
                for c in extra:
                    key = (c.symbol, c.side, c.strategy_name)
                    if key in seen:
                        continue
                    sigs.append(c)
                    seen.add(key)
                logger.info(
                    "opportunity_engine | factor_sleeve merged %d extra candidates", len(extra)
                )
        except Exception as exc:  # noqa: BLE001
            # Defensive: a sleeve failure must never take down the
            # opportunity pipeline. Log and proceed with the original
            # signal list.
            logger.warning("opportunity_engine | factor_sleeve runner failed: %s", exc)

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
        trained_meta_labeler_config=trained_meta_labeler_config,
    )
