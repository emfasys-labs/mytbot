"""
ai/market_context.py
======================
Wave 7 — per-symbol "everything we know right now" snapshot.

``MarketContext`` is the single dataclass the fusion layer
(``ai/fusion.py``) consumes. It is *not* derived from any module —
the caller assembles it from whichever sources are populated. Missing
fields are ``None`` and the fusion layer skips them; nothing crashes
on partial inputs.

Boundary discipline:

- This module imports nothing from ``brokers/``.
- ``MarketContext`` is a plain dataclass; no IO; no LLM calls.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


@dataclass
class StructuredForecast:
    """Fields from Wave 6 (forecast bridge) collapsed for fusion."""

    expected_return: Optional[float] = None
    expected_volatility: Optional[float] = None
    confidence: Optional[float] = None
    horizons_used: tuple[int, ...] = ()


@dataclass
class NewsContext:
    score: Optional[float] = None       # signed sentiment in roughly [-1, 1]
    materiality: Optional[float] = None  # 0..1
    event_type: Optional[str] = None
    rationale: Optional[str] = None


@dataclass
class MacroContext:
    regime_label: Optional[str] = None
    regime_score: Optional[float] = None  # signed market-state score
    breadth: Optional[float] = None
    market_volatility: Optional[float] = None


@dataclass
class GraphContext:
    affected_asset_classes: tuple[str, ...] = ()
    related_symbols: tuple[str, ...] = ()
    upstream_trigger: Optional[str] = None
    propagation_strength: Optional[float] = None


@dataclass
class PortfolioContext:
    has_position: bool = False
    position_side: Optional[str] = None       # "long" | "short"
    gross_exposure_pct: Optional[float] = None
    drawdown_from_hwm_pct: Optional[float] = None


@dataclass
class ExecutionContext:
    last_slippage_bps: Optional[float] = None
    fill_rate: Optional[float] = None         # 0..1
    venue_quality: Optional[float] = None     # 0..1


@dataclass
class AccumulatorContext:
    """Snapshot from ``signals/accumulator.py`` for the symbol."""

    score: Optional[float] = None
    confidence: Optional[float] = None
    aligned_sources: tuple[str, ...] = ()
    conflicting_sources: tuple[str, ...] = ()


@dataclass
class MarketContext:
    """Everything the fusion layer needs to decide for one symbol."""

    symbol: str
    asset_class: str = "other"
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    structured_forecast: Optional[StructuredForecast] = None
    news: Optional[NewsContext] = None
    macro: Optional[MacroContext] = None
    graph: Optional[GraphContext] = None
    portfolio: Optional[PortfolioContext] = None
    execution: Optional[ExecutionContext] = None
    accumulator: Optional[AccumulatorContext] = None

    metadata: dict[str, object] = field(default_factory=dict)


class MarketContextBuilder:
    """Tiny convenience builder so fusion call sites don't repeat boilerplate."""

    @staticmethod
    def from_inputs(
        *,
        symbol: str,
        asset_class: str = "other",
        forecast_decision=None,         # ai.signals.forecast_bridge.ForecastDecision
        news_score: Optional[float] = None,
        news_materiality: Optional[float] = None,
        news_event_type: Optional[str] = None,
        news_rationale: Optional[str] = None,
        regime_label: Optional[str] = None,
        regime_score: Optional[float] = None,
        breadth: Optional[float] = None,
        market_volatility: Optional[float] = None,
        accumulator_net=None,           # signals.accumulator.NetSignal | None
        portfolio_position_side: Optional[str] = None,
        gross_exposure_pct: Optional[float] = None,
        drawdown_from_hwm_pct: Optional[float] = None,
        last_slippage_bps: Optional[float] = None,
        fill_rate: Optional[float] = None,
        venue_quality: Optional[float] = None,
        graph_context: Optional[GraphContext] = None,
        timestamp: Optional[datetime] = None,
    ) -> MarketContext:
        ts = timestamp or datetime.now(timezone.utc)

        sf: Optional[StructuredForecast] = None
        if forecast_decision is not None and getattr(forecast_decision, "used", False):
            sf = StructuredForecast(
                expected_return=getattr(forecast_decision, "expected_return", None),
                expected_volatility=getattr(forecast_decision, "expected_volatility", None),
                confidence=getattr(forecast_decision, "confidence", None),
                horizons_used=tuple(getattr(forecast_decision, "horizons_used", ()) or ()),
            )

        news: Optional[NewsContext] = None
        if any(v is not None for v in (news_score, news_materiality, news_event_type, news_rationale)):
            news = NewsContext(
                score=news_score,
                materiality=news_materiality,
                event_type=news_event_type,
                rationale=news_rationale,
            )

        macro: Optional[MacroContext] = None
        if any(v is not None for v in (regime_label, regime_score, breadth, market_volatility)):
            macro = MacroContext(
                regime_label=regime_label,
                regime_score=regime_score,
                breadth=breadth,
                market_volatility=market_volatility,
            )

        acc: Optional[AccumulatorContext] = None
        if accumulator_net is not None:
            acc = AccumulatorContext(
                score=float(getattr(accumulator_net, "score", 0.0) or 0.0),
                confidence=float(getattr(accumulator_net, "confidence", 0.0) or 0.0),
                aligned_sources=tuple(getattr(accumulator_net, "aligned_sources", ()) or ()),
                conflicting_sources=tuple(getattr(accumulator_net, "conflicting_sources", ()) or ()),
            )

        port: Optional[PortfolioContext] = None
        if any(v is not None for v in (portfolio_position_side, gross_exposure_pct, drawdown_from_hwm_pct)):
            port = PortfolioContext(
                has_position=portfolio_position_side is not None,
                position_side=portfolio_position_side,
                gross_exposure_pct=gross_exposure_pct,
                drawdown_from_hwm_pct=drawdown_from_hwm_pct,
            )

        execu: Optional[ExecutionContext] = None
        if any(v is not None for v in (last_slippage_bps, fill_rate, venue_quality)):
            execu = ExecutionContext(
                last_slippage_bps=last_slippage_bps,
                fill_rate=fill_rate,
                venue_quality=venue_quality,
            )

        return MarketContext(
            symbol=symbol,
            asset_class=asset_class,
            timestamp=ts,
            structured_forecast=sf,
            news=news,
            macro=macro,
            graph=graph_context,
            portfolio=port,
            execution=execu,
            accumulator=acc,
        )
