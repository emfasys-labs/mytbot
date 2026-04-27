"""
D015 regime / market state from M2 cross-section + optional news dispersion.

``ai/regime.py`` remains strategy gating; this module feeds allocator exposure and
dynamic weights. All anchors come from ``allocation.yaml`` ``market_state`` section.

Wave 4 wiring — when ``regime_models.classifier.enabled: true`` in
``config/regime_models.yaml`` AND a fitted ``HMMRegimeClassifier`` artefact
is configured, the heuristic label is *augmented* (not replaced) by the
classifier's prediction. The classifier label is mapped onto the
existing ``RegimeLabel`` vocabulary; heuristic-derived metadata
(symbol_count, insufficient_cross_section) is preserved unchanged.
Operators flip the gate after a paper soak per docs/MODEL_GOVERNANCE.md.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional, cast

import yaml

from config.models import AllocationConfig
from core.models_runtime import MarketStateComponents, PortfolioState, RegimeLabel, RegimeState, clip_decimal
from data.regime_metrics import cross_section_from_feature_rows, fetch_latest_feature_rows, fetch_news_score_dispersion

logger = logging.getLogger(__name__)


# ── Wave 4 wiring ───────────────────────────────────────────────────────────


REGIME_MODELS_DEFAULT_PATH = Path("config/regime_models.yaml")


@dataclass
class _RegimeClassifierGate:
    enabled: bool = False
    artifact_path: Optional[Path] = None
    feature_names: tuple[str, ...] = ()
    min_samples: int = 60


_CLASSIFIER_GATE_UNLOADED = object()
_CLASSIFIER_GATE_CACHE: object = _CLASSIFIER_GATE_UNLOADED
_CLASSIFIER_ARTEFACT_CACHE: object = _CLASSIFIER_GATE_UNLOADED


def _load_regime_classifier_gate() -> _RegimeClassifierGate:
    """Read the YAML once; subsequent calls hit the module cache."""
    global _CLASSIFIER_GATE_CACHE
    if _CLASSIFIER_GATE_CACHE is not _CLASSIFIER_GATE_UNLOADED:
        return _CLASSIFIER_GATE_CACHE  # type: ignore[return-value]
    p = REGIME_MODELS_DEFAULT_PATH
    if not p.exists():
        _CLASSIFIER_GATE_CACHE = _RegimeClassifierGate()
        return _CLASSIFIER_GATE_CACHE  # type: ignore[return-value]
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        logger.warning("regime_state | could not parse %s: %s", p, exc)
        _CLASSIFIER_GATE_CACHE = _RegimeClassifierGate()
        return _CLASSIFIER_GATE_CACHE  # type: ignore[return-value]
    sect = ((raw.get("regime_models") or {}).get("classifier") or {})
    ap = sect.get("artifact_path")
    _CLASSIFIER_GATE_CACHE = _RegimeClassifierGate(
        enabled=bool(sect.get("enabled", False)),
        artifact_path=Path(ap) if ap else None,
        feature_names=tuple(sect.get("feature_names") or ()),
        min_samples=int(sect.get("min_samples", 60)),
    )
    return _CLASSIFIER_GATE_CACHE  # type: ignore[return-value]


def reset_regime_classifier_cache() -> None:
    """Test helper — clear cached gate and artefact."""
    global _CLASSIFIER_GATE_CACHE, _CLASSIFIER_ARTEFACT_CACHE
    _CLASSIFIER_GATE_CACHE = _CLASSIFIER_GATE_UNLOADED
    _CLASSIFIER_ARTEFACT_CACHE = _CLASSIFIER_GATE_UNLOADED


def _load_regime_classifier_artefact(gate: _RegimeClassifierGate):
    """Load the pickled HMMRegimeClassifier on first use; cache thereafter."""
    global _CLASSIFIER_ARTEFACT_CACHE
    if _CLASSIFIER_ARTEFACT_CACHE is not _CLASSIFIER_GATE_UNLOADED:
        return _CLASSIFIER_ARTEFACT_CACHE
    if not gate.enabled or gate.artifact_path is None or not gate.artifact_path.exists():
        _CLASSIFIER_ARTEFACT_CACHE = None
        return None
    try:
        from risk.regime_models import HMMRegimeClassifier

        _CLASSIFIER_ARTEFACT_CACHE = HMMRegimeClassifier.load(gate.artifact_path)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "regime_state | classifier artefact %s could not be loaded: %s — falling back to heuristic",
            gate.artifact_path,
            exc,
        )
        _CLASSIFIER_ARTEFACT_CACHE = None
    return _CLASSIFIER_ARTEFACT_CACHE


# Map classifier vocabulary → RegimeLabel vocabulary. Both share most
# labels; only "trend" needs a remap.
_CLASSIFIER_LABEL_MAP: dict[str, RegimeLabel] = {
    "risk_on": "risk_on",
    "risk_off": "risk_off",
    "trend": "trend_up",
    "range": "range",
    "volatile": "volatile",
    "crash": "crash",
}


def _build_classifier_feature_vector(
    *,
    feature_names: tuple[str, ...],
    comps: MarketStateComponents,
    market_state_score: Decimal,
    breadth_score: Decimal,
    news_conflict: float,
) -> Optional[list[float]]:
    """
    Construct the feature vector the classifier was trained on.

    The supported keys mirror the defaults shipped in
    ``config/regime_models.yaml``. Missing keys are filled with 0.0; if
    the operator trained on a feature we cannot derive at runtime, we
    fall back to 0.0 rather than refuse classification — the
    classifier itself returns ``insufficient_data`` if the dim is wrong.
    """
    if not feature_names:
        return None
    derived: dict[str, float] = {
        "mean_return": float(market_state_score),
        "volatility": float(comps.volatility_structure or comps.chaos_penalty),
        "breadth": float(breadth_score),
        "news_dispersion": float(news_conflict),
        "trend_strength": float(comps.trend_strength),
        "chaos_penalty": float(comps.chaos_penalty),
        "correlation_crowding": float(comps.correlation_crowding),
        "anomaly_breadth": float(comps.anomaly_breadth),
        "macro_clarity": float(comps.macro_clarity),
        "liquidity_state": float(comps.liquidity_state),
    }
    return [float(derived.get(name, 0.0)) for name in feature_names]


def _maybe_apply_classifier(
    *,
    heuristic_label: RegimeLabel,
    insufficient: bool,
    comps: MarketStateComponents,
    market_state_score: Decimal,
    breadth_score: Decimal,
    news_conflict: float,
    meta: dict,
) -> RegimeLabel:
    """
    When the trained classifier is enabled and an artefact is loadable,
    override ``heuristic_label`` with its prediction (mapped to
    ``RegimeLabel``). Otherwise return ``heuristic_label`` unchanged.

    Always populates ``meta`` with diagnostics so the dashboard can show
    which path produced the label.
    """
    meta["regime_classifier_used"] = False
    if insufficient:
        # Never let the classifier override an explicit insufficient_data
        # signal — that would mask data-quality issues.
        return heuristic_label

    gate = _load_regime_classifier_gate()
    if not gate.enabled:
        return heuristic_label

    artefact = _load_regime_classifier_artefact(gate)
    if artefact is None:
        meta["regime_classifier_reason"] = "artefact_unavailable"
        return heuristic_label

    feature_names = artefact.feature_names or gate.feature_names
    feats = _build_classifier_feature_vector(
        feature_names=feature_names,
        comps=comps,
        market_state_score=market_state_score,
        breadth_score=breadth_score,
        news_conflict=news_conflict,
    )
    if not feats:
        meta["regime_classifier_reason"] = "no_features_configured"
        return heuristic_label

    try:
        import numpy as np

        clf_label = artefact.predict_label(np.asarray(feats, dtype=float))
    except Exception as exc:  # noqa: BLE001
        logger.warning("regime_state | classifier predict failed: %s — falling back", exc)
        meta["regime_classifier_reason"] = "predict_failed"
        return heuristic_label

    mapped = _CLASSIFIER_LABEL_MAP.get(clf_label)
    meta["regime_classifier_used"] = True
    meta["regime_classifier_backend"] = getattr(artefact, "backend_", "unknown")
    meta["regime_classifier_label_raw"] = clf_label
    meta["regime_heuristic_label"] = heuristic_label
    if mapped is None:
        meta["regime_classifier_reason"] = "label_unmapped"
        return heuristic_label
    return mapped


def _dec(x: float) -> Decimal:
    return Decimal(str(x))


def _label_from_components(
    comps: MarketStateComponents,
    *,
    insufficient: bool,
) -> RegimeLabel:
    if insufficient:
        return "insufficient_data"
    c = comps
    if c.chaos_penalty > Decimal("0.6"):
        return "volatile"
    if c.trend_strength > Decimal("0.55") and c.correlation_crowding < Decimal("0.45"):
        return "risk_on"
    if c.trend_strength < Decimal("0.35") and c.chaos_penalty > Decimal("0.35"):
        return "risk_off"
    if c.anomaly_breadth > Decimal("0.5"):
        return "volatile"
    return "mixed"


def compute_regime_state_from_inputs(
    *,
    portfolio_state: PortfolioState,
    allocation_cfg: AllocationConfig,
    feature_rows: list[dict[str, Any]],
    news_dispersion: tuple[float, float] | None,
    now: datetime | None = None,
    execution_quality: Decimal | None = None,
    broker_liquidity_score: Decimal | None = None,
) -> RegimeState:
    """
    Build ``RegimeState`` from pre-fetched rows (tests + callers). No silent neutral
    world: missing cross-section yields ``insufficient_data`` when below min symbol count.
    """
    ts = now or datetime.now(timezone.utc)
    ms_cfg = allocation_cfg.market_state
    wc = ms_cfg.components

    min_sym = int(ms_cfg.min_symbols_for_regime)
    insufficient = len(feature_rows) < min_sym

    enr = ms_cfg.liquidity_enrichment
    raw = cross_section_from_feature_rows(
        feature_rows,
        anomaly_volume_z_threshold=float(ms_cfg.anomaly_volume_z_threshold),
        anomaly_rel_dv_threshold=float(ms_cfg.anomaly_rel_dv_threshold),
        broker_liquidity_score=float(broker_liquidity_score) if broker_liquidity_score is not None else None,
        broker_weight=float(enr.broker_depth_weight),
        feature_weight=float(enr.feature_proxy_weight),
    )

    news_conflict = 0.0
    if news_dispersion is not None:
        mean_s, std_s = news_dispersion
        if abs(mean_s) > 1e-6:
            news_conflict = min(1.0, abs(std_s) / (abs(mean_s) + 0.15))
        else:
            news_conflict = min(1.0, std_s * 2.0)
    raw["news_conflict_score"] = news_conflict

    comps = MarketStateComponents(
        trend_strength=_dec(raw["trend_strength"]),
        cross_asset_confirmation=_dec(raw["cross_asset_confirmation"]),
        liquidity_state=_dec(raw["liquidity_state"]),
        macro_clarity=_dec(raw["macro_clarity"]),
        risk_on_breadth=_dec(raw["risk_on_breadth"]),
        chaos_penalty=_dec(raw["chaos_penalty"]),
        correlation_crowding=_dec(raw["correlation_crowding"]),
        volatility_structure=_dec(raw["volatility_structure"]),
        anomaly_breadth=_dec(raw["anomaly_breadth"]),
        news_conflict_score=_dec(raw["news_conflict_score"]),
    )

    score_f = (
        wc.trend_strength * raw["trend_strength"]
        + wc.cross_asset_confirmation * raw["cross_asset_confirmation"]
        + wc.liquidity_state * raw["liquidity_state"]
        + wc.macro_clarity * raw["macro_clarity"]
        + wc.risk_on_breadth * raw["risk_on_breadth"]
        + wc.chaos_penalty * raw["chaos_penalty"]
        + wc.correlation_crowding * raw["correlation_crowding"]
        + wc.volatility_structure * raw["volatility_structure"]
        + wc.anomaly_breadth * raw["anomaly_breadth"]
        + wc.news_conflict_score * news_conflict
    )
    market_state_score = clip_decimal(_dec(score_f), Decimal("-2"), Decimal("2"))

    dd = max(Decimal("0"), portfolio_state.drawdown_from_hwm_pct)
    drawdown_throttle = clip_decimal(Decimal("1") - dd * Decimal("2.5"), Decimal("0.1"), Decimal("1"))

    eq = execution_quality if execution_quality is not None else Decimal("1")
    eq = clip_decimal(eq, Decimal("0"), Decimal("1"))

    breadth = comps.risk_on_breadth + comps.anomaly_breadth * Decimal("0.5")
    breadth_score = clip_decimal(breadth, Decimal("0"), Decimal("1"))

    heuristic_label = _label_from_components(comps, insufficient=insufficient)

    meta: dict[str, str | int | float | bool] = {
        "symbol_count": int(raw["symbol_count"]),
        "insufficient_cross_section": insufficient,
    }
    if insufficient:
        logger.info(
            "regime_state | insufficient_data | symbols=%s min_required=%s",
            raw["symbol_count"],
            min_sym,
        )

    label = _maybe_apply_classifier(
        heuristic_label=heuristic_label,
        insufficient=insufficient,
        comps=comps,
        market_state_score=market_state_score,
        breadth_score=breadth_score,
        news_conflict=news_conflict,
        meta=meta,
    )

    return RegimeState(
        timestamp=ts,
        regime_label=cast(RegimeLabel, label),
        market_state_score=market_state_score,
        drawdown_throttle=drawdown_throttle,
        execution_quality=eq,
        breadth_score=breadth_score,
        components=comps,
        metadata=meta,
    )


async def compute_regime_state_async(
    *,
    portfolio_state: PortfolioState,
    allocation_cfg: AllocationConfig,
    session: Any,
    universe_symbols: list[str],
    timeframe: str = "1h",
    now: datetime | None = None,
    execution_quality: Decimal | None = None,
    broker_liquidity_score: Decimal | None = None,
) -> RegimeState:
    """Load latest feature rows + news dispersion from DB, then compute regime."""
    from sqlalchemy.ext.asyncio import AsyncSession

    assert isinstance(session, AsyncSession)
    rows = await fetch_latest_feature_rows(session, universe_symbols, timeframe)
    news = await fetch_news_score_dispersion(
        session, lookback_hours=int(allocation_cfg.market_state.news_lookback_hours)
    )
    return compute_regime_state_from_inputs(
        portfolio_state=portfolio_state,
        allocation_cfg=allocation_cfg,
        feature_rows=rows,
        news_dispersion=news,
        now=now,
        execution_quality=execution_quality,
        broker_liquidity_score=broker_liquidity_score,
    )


def compute_regime_state(
    *,
    portfolio_state: PortfolioState,
    allocation_cfg: AllocationConfig | None = None,
    now: datetime | None = None,
) -> RegimeState:
    """Backward-compatible entry: no DB; marks insufficient_data for allocator tests."""
    from config.loaders import load_allocation

    cfg = allocation_cfg or load_allocation()
    return compute_regime_state_from_inputs(
        portfolio_state=portfolio_state,
        allocation_cfg=cfg,
        feature_rows=[],
        news_dispersion=None,
        now=now,
        execution_quality=Decimal("1"),
    )
