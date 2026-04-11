"""
D015 regime / market state from M2 cross-section + optional news dispersion.

``ai/regime.py`` remains strategy gating; this module feeds allocator exposure and
dynamic weights. All anchors come from ``allocation.yaml`` ``market_state`` section.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, cast

from config.models import AllocationConfig
from core.models_runtime import MarketStateComponents, PortfolioState, RegimeLabel, RegimeState, clip_decimal
from data.regime_metrics import cross_section_from_feature_rows, fetch_latest_feature_rows, fetch_news_score_dispersion

logger = logging.getLogger(__name__)


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

    label = _label_from_components(comps, insufficient=insufficient)

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
