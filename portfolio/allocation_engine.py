"""
portfolio/allocation_engine.py
================================
D015 global opportunity replacement allocator: gross exposure, softmax weights,
replacement advantage vs hold scores, safety bounds from ``profile_modes.yaml``.

Coexists with ``portfolio/allocator.py`` (legacy sizing) until integration flag is on.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timezone
from decimal import Decimal

from config.loaders import load_allocation, load_profile_modes
from config.models import AllocationConfig, ProfileModesConfig
from core.models_runtime import (
    AllocationDecision,
    AllocationTarget,
    Opportunity,
    PortfolioState,
    ProfileMode,
    RegimeState,
    ReplacementCandidate,
    clip_decimal,
)
from core.signal_math import bounded_sigmoid
from portfolio.d015_hold_switch import compute_hold_score, compute_switching_cost_score
from portfolio.d015_replacement_context import ReplacementContext, churn_penalty_for_pair
from signals.d015_weights import (
    aggression_multiplier_for_mode,
    concentration_exponent_for_mode,
    replacement_sensitivity_for_mode,
)
from portfolio.adaptive_sizing import compute_adaptive_max_weight

logger = logging.getLogger(__name__)


# ── Wave 8 wiring: vol-targeting + drawdown overlay (gated off) ────────────


_PORTFOLIO_OPT_CFG_UNLOADED = object()
_PORTFOLIO_OPT_CFG_CACHE: object = _PORTFOLIO_OPT_CFG_UNLOADED


def _get_default_portfolio_optimisation_config():
    """Lazy loader for ``config/portfolio_optimisation.yaml``. Cached."""
    global _PORTFOLIO_OPT_CFG_CACHE
    if _PORTFOLIO_OPT_CFG_CACHE is not _PORTFOLIO_OPT_CFG_UNLOADED:
        return _PORTFOLIO_OPT_CFG_CACHE
    try:
        from portfolio.optimizers import PortfolioOptimisationConfig

        _PORTFOLIO_OPT_CFG_CACHE = PortfolioOptimisationConfig.load()
    except Exception as exc:  # noqa: BLE001
        logger.warning("allocation_engine | portfolio_optimisation load failed: %s", exc)
        _PORTFOLIO_OPT_CFG_CACHE = None
    return _PORTFOLIO_OPT_CFG_CACHE


def reset_portfolio_optimisation_cache() -> None:
    """Test helper — clears the cached PortfolioOptimisationConfig."""
    global _PORTFOLIO_OPT_CFG_CACHE
    _PORTFOLIO_OPT_CFG_CACHE = _PORTFOLIO_OPT_CFG_UNLOADED


def _apply_wave8_vol_targeting_overlay(
    *,
    ge: Decimal,
    regime_state: RegimeState,
    portfolio_state: PortfolioState,
    cfg,  # PortfolioOptimisationConfig
) -> tuple[Decimal, dict[str, float | bool]]:
    """
    Multiply the gross-exposure target by ``combined_scale`` from
    ``portfolio.vol_targeting`` when the overlay is enabled.

    Inputs:
      - target_vol from config
      - realised_vol from ``regime_state.metadata['market_volatility']``
        (set by the demand engine in Wave 3)
      - drawdown from ``portfolio_state.drawdown_from_hwm_pct``

    Defensive: any failure returns ``ge`` unchanged with diagnostic
    metadata explaining why.
    """
    overlay = getattr(cfg, "vol_targeting_overlay", None)
    if overlay is None or not getattr(overlay, "enabled", False):
        return ge, {"wave8_vol_overlay_used": False}

    try:
        from portfolio.vol_targeting import combined_scale

        rv_raw = (regime_state.metadata or {}).get("market_volatility")
        try:
            realised_vol = float(rv_raw) if rv_raw is not None else None
        except (TypeError, ValueError):
            realised_vol = None
        try:
            drawdown = float(portfolio_state.drawdown_from_hwm_pct or 0.0)
        except (TypeError, ValueError):
            drawdown = 0.0

        result = combined_scale(
            target_vol=overlay.target_vol,
            realised_vol=realised_vol,
            drawdown=drawdown,
            soft_drawdown=overlay.soft_drawdown,
            hard_drawdown=overlay.hard_drawdown,
            floor=overlay.drawdown_floor,
            min_scale=overlay.min_scale,
            max_scale=overlay.max_scale,
        )
        scale = float(result.scale)
        if not (scale > 0):
            return ge, {
                "wave8_vol_overlay_used": False,
                "wave8_vol_overlay_reason": "non_positive_scale",
            }
        new_ge = ge * Decimal(str(scale))
        meta: dict[str, float | bool] = {
            "wave8_vol_overlay_used": True,
            "wave8_vol_overlay_scale": scale,
        }
        if result.vol_component is not None:
            meta["wave8_vol_overlay_vol_component"] = float(result.vol_component)
        if result.drawdown_component is not None:
            meta["wave8_vol_overlay_drawdown_component"] = float(result.drawdown_component)
        meta["wave8_vol_overlay_realised_vol"] = float(realised_vol) if realised_vol is not None else 0.0
        meta["wave8_vol_overlay_drawdown"] = float(drawdown)
        return new_ge, meta
    except Exception as exc:  # noqa: BLE001
        logger.warning("allocation_engine | wave8 vol overlay failed: %s", exc)
        return ge, {"wave8_vol_overlay_used": False, "wave8_vol_overlay_error": str(exc)}


def _resolve_mode(portfolio_state: PortfolioState, profile_cfg: ProfileModesConfig) -> ProfileMode:
    m = portfolio_state.mode
    if m in profile_cfg.modes:
        return m
    return profile_cfg.defaults.active_mode


def _capital_slider(portfolio_state: PortfolioState) -> Decimal:
    raw = portfolio_state.metadata.get("capital_pct", 1.0)
    try:
        return clip_decimal(Decimal(str(float(raw))), Decimal("0"), Decimal("1"))
    except (TypeError, ValueError, ArithmeticError):
        return Decimal("1")


def _volatility_overlay(regime_state: RegimeState) -> tuple[Decimal, dict[str, float]]:
    """
    Portfolio-level volatility throttle.

    Reads optional `regime_state.metadata["market_volatility"]` (from demand engine
    cross-asset graph). Falls back to regime chaos proxy when unavailable.
    """
    md = regime_state.metadata or {}
    try:
        mvol = float(md.get("market_volatility", 0.0) or 0.0)
    except (TypeError, ValueError):
        mvol = 0.0
    if mvol <= 0:
        try:
            mvol = max(0.0, float(regime_state.components.chaos_penalty))
        except Exception:  # noqa: BLE001
            mvol = 0.0
    try:
        coverage = float(md.get("cross_asset_coverage", 1.0) or 1.0)
    except (TypeError, ValueError):
        coverage = 1.0
    coverage = max(0.0, min(1.0, coverage))
    # Fallback values chosen to be conservative but non-disruptive.
    threshold = 0.010
    slope = 9.0
    floor = 0.65
    raw = 1.0 - max(0.0, mvol - threshold) * slope
    if coverage < 0.45:
        raw *= 0.95
    overlay = Decimal(str(max(floor, min(1.05, raw))))
    return overlay, {
        "market_volatility": mvol,
        "cross_asset_coverage": coverage,
        "vol_overlay_raw": raw,
        "vol_overlay_applied": float(overlay),
    }


def build_allocation_decision(
    *,
    opportunities: list[Opportunity],
    portfolio_state: PortfolioState,
    regime_state: RegimeState,
    allocation_cfg: AllocationConfig | None = None,
    profile_cfg: ProfileModesConfig | None = None,
    now: datetime | None = None,
    replacement_context: ReplacementContext | None = None,
) -> AllocationDecision:
    if allocation_cfg is None:
        allocation_cfg = load_allocation()
    if profile_cfg is None:
        profile_cfg = load_profile_modes()

    ts = now or datetime.now(timezone.utc)
    mode = _resolve_mode(portfolio_state, profile_cfg)

    if not allocation_cfg.allocator.enabled:
        return AllocationDecision(
            timestamp=ts,
            mode=mode,
            gross_exposure_target=Decimal("0"),
            net_exposure_target=Decimal("0"),
            capital_deployment_target=Decimal("0"),
            rationale="allocator.disabled",
            metadata={"d015": True},
        )

    cap_slider = _capital_slider(portfolio_state)
    agg = aggression_multiplier_for_mode(profile_cfg, mode, regime_state)
    best = max((o.opportunity_score for o in opportunities), default=Decimal("0"))
    sh = allocation_cfg.gross_exposure.shaping
    w_ms = Decimal(str(sh.market_state_weight))
    w_br = Decimal(str(sh.breadth_weight))
    sc = Decimal(str(sh.aggregate_scale))
    sig_arg = (best + regime_state.market_state_score * w_ms + regime_state.breadth_score * w_br) * sc
    ge_shape = bounded_sigmoid(
        clip_decimal(sig_arg, Decimal(str(sh.sigmoid_clip_min)), Decimal(str(sh.sigmoid_clip_max)))
    )
    ge = cap_slider * agg * ge_shape * regime_state.execution_quality * regime_state.drawdown_throttle
    vol_overlay, vol_meta = _volatility_overlay(regime_state)
    ge = ge * vol_overlay
    # Wave 8 — optional vol-targeting + drawdown overlay (gated off by default).
    wave8_cfg = _get_default_portfolio_optimisation_config()
    wave8_meta: dict[str, float | bool] = {"wave8_vol_overlay_used": False}
    if wave8_cfg is not None:
        ge, wave8_meta = _apply_wave8_vol_targeting_overlay(
            ge=ge,
            regime_state=regime_state,
            portfolio_state=portfolio_state,
            cfg=wave8_cfg,
        )
    try:
        demand_score = float((regime_state.metadata or {}).get("demand_score", 0.0) or 0.0)
    except (TypeError, ValueError):
        demand_score = 0.0
    demand_trend = str((regime_state.metadata or {}).get("demand_trend", "flat"))
    try:
        demand_confidence = float((regime_state.metadata or {}).get("demand_confidence", 0.0) or 0.0)
    except (TypeError, ValueError):
        demand_confidence = 0.0

    max_ge = Decimal(str(profile_cfg.safety_bounds.absolute_max_gross_exposure.get(mode, 2.0)))
    ge = clip_decimal(ge, Decimal("0"), max_ge)

    conc = concentration_exponent_for_mode(profile_cfg, mode, regime_state)
    lam = float(allocation_cfg.position_weights.lambda_)
    conc_f = float(conc)

    sorted_opps = sorted(opportunities, key=lambda o: o.opportunity_score, reverse=True)
    raw_ws: list[float] = []
    for o in sorted_opps:
        raw_ws.append(math.exp(lam * float(o.opportunity_score) * conc_f))
    s = sum(raw_ws) or 1.0
    norm_ws = [w / s for w in raw_ws]

    targets: list[AllocationTarget] = []
    pos_set = {p.symbol for p in portfolio_state.positions}
    open_syms: list[str] = []
    for rank, (o, nw) in enumerate(zip(sorted_opps, norm_ws, strict=True), start=1):
        dynamic_max_w = compute_adaptive_max_weight(
            opportunity=o,
            regime_state=regime_state,
            mode=mode,
            profile_cfg=profile_cfg,
            target_risk_budget=0.015,
        )
        tw = clip_decimal(Decimal(str(nw)) * ge, Decimal("0"), dynamic_max_w)
        tn = clip_decimal(portfolio_state.nav * tw, Decimal("0"), portfolio_state.nav * max_ge)
        omd = dict(o.metadata) if isinstance(o.metadata, dict) else {}
        sn = omd.get("strategy")
        if sn is not None and not isinstance(sn, str):
            sn = str(sn).strip() or None
        elif isinstance(sn, str):
            sn = sn.strip() or None
        targets.append(
            AllocationTarget(
                symbol=o.symbol,
                target_weight=tw,
                target_notional=tn,
                target_leverage=Decimal("1"),
                side=o.side,
                source_opportunity_score=o.opportunity_score,
                priority_rank=rank,
                strategy_name=sn,
                metadata={**omd, "d015": True},
            )
        )
        if o.symbol not in pos_set:
            open_syms.append(o.symbol)

    opp_by_sym = {o.symbol: o for o in opportunities}
    hold_ranked: list[tuple] = []
    for pos in portfolio_state.positions:
        hs = compute_hold_score(pos, opp_by_sym, allocation_cfg)
        hold_ranked.append((pos, hs))
    hold_ranked.sort(key=lambda x: x[1])

    repl: list[ReplacementCandidate] = []
    close_syms: list[str] = []
    rsens = replacement_sensitivity_for_mode(profile_cfg, mode, regime_state)
    thr = Decimal(str(allocation_cfg.replacement_logic.thresholds.minimum_replacement_advantage))
    em_thr = Decimal(str(allocation_cfg.replacement_logic.thresholds.extreme_opportunity_threshold))
    em_ok = allocation_cfg.replacement_logic.thresholds.emergency_override_on_extreme_opportunity

    min_iv = int(allocation_cfg.allocator.min_replacement_interval_seconds)
    churn_cfg = allocation_cfg.replacement_logic.churn

    for opp in sorted_opps:
        if not hold_ranked:
            break
        worst_pos, worst_hold = hold_ranked[0]
        if opp.symbol == worst_pos.symbol:
            continue
        if replacement_context is not None and min_iv > 0:
            last = replacement_context.last_event_at_by_symbol.get(worst_pos.symbol)
            if last is not None and (ts - last).total_seconds() < float(min_iv):
                continue
        sw = compute_switching_cost_score(
            opportunity=opp,
            position=worst_pos,
            allocation_cfg=allocation_cfg,
        )
        if churn_cfg.enabled and replacement_context is not None:
            sw = sw + churn_penalty_for_pair(
                worst_pos.symbol,
                opp.symbol,
                recent_events=replacement_context.recent_events,
                max_events=int(churn_cfg.max_recent_events),
                penalty_per_event=Decimal(str(churn_cfg.penalty_per_recent_event)),
            )
        sw = clip_decimal(sw, Decimal("0"), Decimal("1"))
        adv = opp.opportunity_score - worst_hold - sw
        eff = adv * rsens
        if eff > thr or (em_ok and opp.opportunity_score >= em_thr):
            repl.append(
                ReplacementCandidate(
                    new_symbol=opp.symbol,
                    old_symbol=worst_pos.symbol,
                    new_opportunity_score=opp.opportunity_score,
                    old_hold_score=worst_hold,
                    switching_cost_score=sw,
                    replacement_advantage=eff,
                    recommended_action="replace",
                    reason="replacement_advantage",
                    metadata={"d015": True},
                )
            )
            close_syms.append(worst_pos.symbol)
            hold_ranked.pop(0)

    hold_syms = [p.symbol for p in portfolio_state.positions if p.symbol not in close_syms]
    deploy = portfolio_state.nav * ge * cap_slider

    return AllocationDecision(
        timestamp=ts,
        mode=mode,
        gross_exposure_target=ge,
        net_exposure_target=ge,
        capital_deployment_target=clip_decimal(deploy, Decimal("0"), portfolio_state.nav * max_ge),
        allocation_targets=targets,
        open_symbols=open_syms,
        close_symbols=close_syms,
        hold_symbols=hold_syms,
        replacement_candidates=repl,
        rationale="d015.global_opportunity_replacement",
        metadata={
            "d015": True,
            "opportunity_count": len(opportunities),
            "position_count": len(portfolio_state.positions),
            **vol_meta,
            **wave8_meta,
            "demand_score": demand_score,
            "demand_trend": demand_trend,
            "demand_confidence": demand_confidence,
        },
    )
