"""
Per-symbol raw signal collection and strategy_candidate_log row assembly (D033).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from ai.regime import filter_by_allowed_strategies
from signals.engine import RawSignal
from system.adaptive_regime_weights import (
    compute_multiplier as compute_regime_multiplier,
    strategy_regime_multiplier,
)
from system.strategy_candidate_log import row as strategy_candidate_row


def apply_regime_filter_with_logs(
    raw_candidates: list[RawSignal],
    *,
    symbol: str,
    ai_result: Any,
    ai_pipeline: Any,
    sc_rows: list[dict[str, Any]],
    loop_iteration: int | None,
) -> list[RawSignal]:
    """``filter_by_allowed_strategies`` with one ``filtered_regime`` row per dropped raw."""
    if ai_result is None or ai_pipeline is None:
        return raw_candidates
    allowed = ai_pipeline.allowed_strategy_names(ai_result.macro_regime)
    pre = list(raw_candidates)
    out = filter_by_allowed_strategies(pre, allowed)
    allowed_pairs = {(r.symbol, r.strategy) for r in out}
    for r in pre:
        if (r.symbol, r.strategy) in allowed_pairs:
            continue
        sc_rows.append(
            strategy_candidate_row(
                symbol=symbol,
                strategy=str(r.strategy),
                side=str(r.side) if r.side else None,
                confidence=r.confidence,
                status="filtered_regime",
                reason="macro_regime_gate",
                loop_iteration=loop_iteration,
            )
        )
    return out


def apply_regime_weighting(
    raw_candidates: list[RawSignal],
    *,
    symbol: str,
    regime_label: str | None = None,
    market_features: dict[str, Any] | None = None,
    min_confidence: float,
    deployment_pressure: float = 0.0,
    sc_rows: list[dict[str, Any]],
    loop_iteration: int | None,
) -> list[RawSignal]:
    """Scale each candidate's confidence by a multiplier **computed live**
    from continuous market features, and drop any whose scaled confidence
    falls below ``min_confidence``.

    The multiplier is a smooth function of feature intensity, not a
    discrete table lookup — mean-reversion's fade in a weak trend is
    mild and in a strong trend it's heavy, automatically, with no
    threshold flip. See
    :func:`system.adaptive_regime_weights.compute_multiplier`.

    Preferred input: ``market_features`` (dict of live
    ``RegimeState.components`` values). Falls back to the legacy
    label-based path via ``regime_label`` when features aren't
    available — useful for older callers / tests but loses the
    continuous-intensity benefit.

    Behaviour:
      * No features AND no label → passthrough.
      * Multiplier exactly 1.0 → passthrough.
      * Multiplier ≠ 1.0 → confidence scaled, metadata stamped; signals
        whose scaled confidence falls below ``min_confidence`` are
        dropped with a ``filtered_regime_weight`` row in the candidate
        log.
    """
    if not raw_candidates:
        return raw_candidates
    label = str(regime_label or "").strip().lower()
    feats = market_features if isinstance(market_features, dict) else None
    if feats is None and not label:
        return raw_candidates
    try:
        pressure = max(0.0, min(1.0, float(deployment_pressure)))
    except (TypeError, ValueError):
        pressure = 0.0
    # When the operator's slider is far above actual deployment, relax only
    # the post-regime confidence floor. The multiplier still fades signals;
    # this just stops borderline candidates from starving breadth.
    effective_min_confidence = max(0.25, float(min_confidence) - (0.30 * pressure))

    kept: list[RawSignal] = []
    for r in raw_candidates:
        # Live-feature path first; only fall back to label-based when
        # no features supplied.
        if feats is not None:
            mult = compute_regime_multiplier(str(r.strategy), feats)
        else:
            mult = strategy_regime_multiplier(str(r.strategy), label)
        try:
            mult_f = float(mult)
        except (TypeError, ValueError):
            mult_f = 1.0
        if mult_f == 1.0:
            kept.append(r)
            continue
        original = float(r.confidence or 0.0)
        scaled = max(0.0, min(1.0, original * mult_f))
        md = dict(getattr(r, "metadata", None) or {})
        md["regime_mult"] = round(mult_f, 4)
        md["regime_label"] = label or None
        md["regime_features_present"] = list(feats.keys()) if feats else []
        md["confidence_pre_regime"] = round(original, 6)
        r.metadata = md
        if scaled < effective_min_confidence:
            sc_rows.append(
                strategy_candidate_row(
                    symbol=symbol,
                    strategy=str(r.strategy),
                    side=str(r.side) if r.side else None,
                    confidence=Decimal(str(round(scaled, 6))),
                    status="filtered_regime_weight",
                    reason=(
                        f"regime_fade:features:mult={mult_f:.2f}"
                        if feats is not None
                        else f"regime_fade:{label}:mult={mult_f:.2f}"
                    ),
                    loop_iteration=loop_iteration,
                    metadata={
                        "regime_label": label or None,
                        "regime_mult": round(mult_f, 4),
                        "regime_features_present": (
                            list(feats.keys()) if feats else []
                        ),
                        "confidence_pre_regime": round(original, 6),
                        "confidence_post_regime": round(scaled, 6),
                        "min_confidence": float(min_confidence),
                        "effective_min_confidence": round(effective_min_confidence, 6),
                        "deployment_pressure": round(pressure, 6),
                    },
                )
            )
            continue
        r.confidence = scaled
        kept.append(r)
    return kept


def collect_raw_signals_for_symbol(
    *,
    symbol: str,
    df: Any,
    sym_ac: str,
    momentum: Any,
    mean_rev: Any,
    volume_flow: Any,
    volatility_regime: Any,
    event_driven: Any,
    regime_rotation: Any,
    ai_result: Any,
    demand_score: float,
    demand_trend: str,
    demand_confidence: float,
    loop_iteration: int | None,
) -> tuple[list[RawSignal], list[dict[str, Any]]]:
    """Build ``raw_candidates`` and pre-regime strategy_candidate_log rows (no_setup / skipped)."""
    sc_rows: list[dict[str, Any]] = []
    raw_candidates: list[RawSignal] = []

    if momentum.enabled and momentum.supports_asset_class(sym_ac):
        m_sig = momentum.generate_signal(symbol, df)
        if m_sig is None:
            mm: dict = {}
            if hasattr(momentum, "no_setup_snapshot"):
                try:
                    mm = momentum.no_setup_snapshot(symbol, df)  # type: ignore[union-attr]
                except Exception:  # noqa: BLE001
                    mm = {}
            sc_rows.append(
                strategy_candidate_row(
                    symbol=symbol,
                    strategy=momentum.name,
                    status="no_setup",
                    reason="no_signal",
                    loop_iteration=loop_iteration,
                    metadata=mm or None,
                )
            )
        else:
            raw_candidates.append(m_sig)
    if mean_rev.enabled and mean_rev.supports_asset_class(sym_ac):
        r_sig = mean_rev.generate_signal(symbol, df)
        if r_sig is None:
            sc_rows.append(
                strategy_candidate_row(
                    symbol=symbol,
                    strategy=mean_rev.name,
                    status="no_setup",
                    reason="no_signal",
                    loop_iteration=loop_iteration,
                )
            )
        else:
            raw_candidates.append(r_sig)
    if volume_flow.enabled and volume_flow.supports_asset_class(sym_ac):
        vf_sig = volume_flow.generate_signal(symbol, df)
        if vf_sig is None:
            vfm: dict = {}
            if hasattr(volume_flow, "no_setup_snapshot"):
                try:
                    vfm = volume_flow.no_setup_snapshot(symbol, df)  # type: ignore[union-attr]
                except Exception:  # noqa: BLE001
                    vfm = {}
            sc_rows.append(
                strategy_candidate_row(
                    symbol=symbol,
                    strategy=volume_flow.name,
                    status="no_setup",
                    reason="no_signal",
                    loop_iteration=loop_iteration,
                    metadata=vfm or None,
                )
            )
        else:
            raw_candidates.append(vf_sig)
    if volatility_regime.enabled and volatility_regime.supports_asset_class(sym_ac):
        vol_sig = volatility_regime.generate_signal(symbol, df)
        if vol_sig is None:
            vrs: dict = {}
            if hasattr(volatility_regime, "no_setup_snapshot"):
                try:
                    vrs = volatility_regime.no_setup_snapshot(symbol, df)  # type: ignore[union-attr]
                except Exception:  # noqa: BLE001
                    vrs = {}
            sc_rows.append(
                strategy_candidate_row(
                    symbol=symbol,
                    strategy=volatility_regime.name,
                    status="no_setup",
                    reason="no_signal",
                    loop_iteration=loop_iteration,
                    metadata=vrs or None,
                )
            )
        else:
            raw_candidates.append(vol_sig)

    if not event_driven.enabled:
        pass
    elif ai_result is None:
        sc_rows.append(
            strategy_candidate_row(
                symbol=symbol,
                strategy=event_driven.name,
                status="skipped",
                reason="ai_result_unavailable",
                loop_iteration=loop_iteration,
                metadata={
                    "near_miss_kind": "event_driven_news",
                    "near_miss_primary": "ai_result_unavailable",
                    "ai_result_present": False,
                    "symbol_news_context_present": False,
                },
            )
        )
    else:
        ev_score = ai_result.news_scores.get(symbol)
        ev_detail = ai_result.news_details.get(symbol)
        ev_shock = float((event_driven.config or {}).get("shock_threshold", 0.45))
        ev_sig = event_driven.generate_from_context(
            symbol=symbol,
            asset_class=sym_ac,
            news_score=ev_score,
            news_detail=ev_detail,
            macro_regime=ai_result.macro_regime,
            macro_confidence=ai_result.macro_confidence,
        )
        if ev_sig is None:
            ns = ev_score
            if ns is None:
                sc_rows.append(
                    strategy_candidate_row(
                        symbol=symbol,
                        strategy=event_driven.name,
                        status="no_setup",
                        reason="no_news_context_for_symbol",
                        loop_iteration=loop_iteration,
                        metadata={
                            "near_miss_kind": "event_driven_news",
                            "near_miss_primary": "no_symbol_news_context",
                            "ai_result_present": True,
                            "symbol_news_context_present": False,
                            "shock_threshold": ev_shock,
                            "event_triggered": False,
                            "reason_detail": "no per-symbol news score in AI result",
                        },
                    )
                )
            else:
                fns = float(ns)
                sc_rows.append(
                    strategy_candidate_row(
                        symbol=symbol,
                        strategy=event_driven.name,
                        status="no_setup",
                        reason="event_not_triggered",
                        loop_iteration=loop_iteration,
                        metadata={
                            "near_miss_kind": "event_driven_news",
                            "near_miss_primary": "below_shock_threshold",
                            "ai_result_present": True,
                            "symbol_news_context_present": True,
                            "symbol_news_score": fns,
                            "shock_threshold": ev_shock,
                            "event_triggered": False,
                            "reason_detail": f"|score| {abs(fns):.4f} below shock {ev_shock}",
                        },
                    )
                )
        else:
            raw_candidates.append(ev_sig)

    if regime_rotation.enabled:
        rr_sig = regime_rotation.generate_from_demand(
            symbol=symbol,
            asset_class=sym_ac,
            demand_score=demand_score,
            demand_trend=demand_trend,
            demand_confidence=demand_confidence,
        )
        if rr_sig is None:
            sc_rows.append(
                strategy_candidate_row(
                    symbol=symbol,
                    strategy=regime_rotation.name,
                    status="no_setup",
                    reason="regime_rotation_not_triggered",
                    loop_iteration=loop_iteration,
                )
            )
        else:
            raw_candidates.append(rr_sig)

    return raw_candidates, sc_rows
