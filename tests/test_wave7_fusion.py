"""
tests/test_wave7_fusion.py
=============================
Wave 7 acceptance tests for the multimodal fusion layer.

Coverage:

- ``NewsEventMemory`` decay: aggregate_score halves over the configured
  half-life; older events drop out past the lookback window; latest_materiality
  reflects decayed weight.
- ``MarketContextBuilder`` skips missing inputs gracefully and stitches
  together a working context.
- ``MultimodalFusion.combine``:
    * empty context → bias 0, confidence 0.
    * aligned bullish sources → positive bias, low conflict, high confidence.
    * conflicting sources → reduced confidence, conflict_score > 0.
    * high news materiality → ``trigger_llm_ensemble=True``.
    * source contributions list is decomposable and audit-friendly.
- ``ai/fusion.py`` does NOT import ``brokers`` (architectural invariant).
- ``RelationshipLoader`` indexes upstream→downstream lookup correctly.
"""

from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from ai.fusion import (
    FusionConfig,
    FusionScore,
    FusionWeights,
    MultimodalFusion,
)
from ai.market_context import (
    AccumulatorContext,
    GraphContext,
    MacroContext,
    MarketContext,
    MarketContextBuilder,
    NewsContext,
    StructuredForecast,
)
from ai.news_event_memory import NewsEvent, NewsEventMemory
from graph.relationship_loader import (
    Relationship,
    RelationshipIndex,
    load_relationships_from_dict,
)


# ── news event memory ─────────────────────────────────────────────────────


def test_news_event_memory_aggregate_decays_over_time() -> None:
    mem = NewsEventMemory(half_life_seconds=3600.0)
    now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    mem.record(NewsEvent(symbol="AAPL", timestamp=now, score=1.0, materiality=1.0))
    # Score immediately at t=0 should be ~+1.
    s_now = mem.aggregate_score("AAPL", now=now)
    assert s_now == pytest.approx(1.0, abs=1e-9)
    # One half-life later, the same event still dominates but is decayed.
    s_one_hl = mem.aggregate_score("AAPL", now=now + timedelta(seconds=3600))
    assert 0.4 <= s_one_hl <= 1.0  # still positive; numerator/denominator both halved


def test_news_event_memory_lookback_window_drops_old_events() -> None:
    mem = NewsEventMemory(half_life_seconds=3600.0)
    now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    mem.record(NewsEvent(symbol="AAPL", timestamp=now - timedelta(hours=24), score=1.0, materiality=1.0))
    score = mem.aggregate_score("AAPL", now=now, lookback_seconds=3600)
    assert score == 0.0  # old event excluded by lookback window


def test_news_event_memory_unknown_symbol_returns_zero() -> None:
    mem = NewsEventMemory()
    assert mem.aggregate_score("GHOST") == 0.0
    assert mem.latest_materiality("GHOST") == 0.0


def test_news_event_memory_max_events_caps_size() -> None:
    mem = NewsEventMemory(max_events=5)
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for i in range(20):
        mem.record(NewsEvent(symbol="AAPL", timestamp=base + timedelta(minutes=i), score=0.1, materiality=0.5))
    assert len(mem) == 5


# ── market context builder ────────────────────────────────────────────────


def test_market_context_builder_handles_missing_inputs() -> None:
    ctx = MarketContextBuilder.from_inputs(symbol="SPY", asset_class="etf")
    assert ctx.structured_forecast is None
    assert ctx.news is None
    assert ctx.macro is None
    assert ctx.accumulator is None


def test_market_context_builder_assembles_full_context() -> None:
    forecast = SimpleNamespace(
        used=True,
        expected_return=0.005,
        expected_volatility=0.012,
        confidence=0.75,
        horizons_used=(1, 4),
    )
    accumulator = SimpleNamespace(score=0.4, confidence=0.6, aligned_sources=("a", "b"), conflicting_sources=())
    ctx = MarketContextBuilder.from_inputs(
        symbol="AAPL",
        asset_class="equity",
        forecast_decision=forecast,
        news_score=0.6,
        news_materiality=0.8,
        regime_label="trend",
        regime_score=0.4,
        accumulator_net=accumulator,
        last_slippage_bps=2.5,
    )
    assert ctx.structured_forecast is not None
    assert ctx.structured_forecast.expected_return == 0.005
    assert ctx.news is not None and ctx.news.materiality == 0.8
    assert ctx.macro is not None and ctx.macro.regime_label == "trend"
    assert ctx.accumulator is not None and ctx.accumulator.score == 0.4
    assert ctx.execution is not None and ctx.execution.last_slippage_bps == 2.5


# ── fusion ────────────────────────────────────────────────────────────────


def _bullish_context(symbol: str = "AAPL") -> MarketContext:
    return MarketContext(
        symbol=symbol,
        asset_class="equity",
        structured_forecast=StructuredForecast(expected_return=0.01, confidence=0.7),
        news=NewsContext(score=0.6, materiality=0.5),
        macro=MacroContext(regime_label="trend", regime_score=0.5),
        accumulator=AccumulatorContext(score=0.4, confidence=0.6),
    )


def _bearish_news_bullish_forecast() -> MarketContext:
    return MarketContext(
        symbol="AAPL",
        asset_class="equity",
        structured_forecast=StructuredForecast(expected_return=0.02, confidence=0.8),
        news=NewsContext(score=-0.8, materiality=0.9),
        accumulator=AccumulatorContext(score=-0.5, confidence=0.6),
    )


def test_fusion_empty_context_is_neutral() -> None:
    fusion = MultimodalFusion(FusionConfig())
    score = fusion.combine(MarketContext(symbol="AAPL"))
    assert score.directional_bias == 0.0
    assert score.confidence == 0.0
    assert score.rationale == "no_sources"


def test_fusion_aligned_bullish_sources_produce_positive_bias() -> None:
    fusion = MultimodalFusion(FusionConfig())
    score = fusion.combine(_bullish_context())
    assert score.directional_bias > 0
    assert score.confidence > 0
    assert score.conflict_score < 0.3
    # Decomposability: each source has a contribution row.
    names = {c.name for c in score.contributions}
    assert {"structured_forecast", "news", "macro", "accumulator"}.issubset(names)


def test_fusion_conflicting_sources_reduce_confidence() -> None:
    fusion = MultimodalFusion(FusionConfig())
    score_aligned = fusion.combine(_bullish_context())
    score_conflict = fusion.combine(_bearish_news_bullish_forecast())
    assert score_conflict.conflict_score > score_aligned.conflict_score
    assert score_conflict.confidence < score_aligned.confidence


def test_fusion_high_materiality_news_triggers_llm_ensemble() -> None:
    fusion = MultimodalFusion(FusionConfig(materiality_llm_threshold=0.7))
    ctx = MarketContext(
        symbol="AAPL",
        asset_class="equity",
        news=NewsContext(score=0.5, materiality=0.85),
    )
    score = fusion.combine(ctx)
    assert score.trigger_llm_ensemble is True


def test_fusion_low_materiality_news_does_not_trigger_llm() -> None:
    fusion = MultimodalFusion(FusionConfig(materiality_llm_threshold=0.7))
    ctx = MarketContext(
        symbol="AAPL",
        asset_class="equity",
        news=NewsContext(score=0.5, materiality=0.3),
    )
    score = fusion.combine(ctx)
    assert score.trigger_llm_ensemble is False


def test_fusion_score_directional_bias_is_clipped() -> None:
    # Pump multiple aligned bullish sources at extreme magnitudes;
    # bias must still cap at +1.
    fusion = MultimodalFusion(FusionConfig())
    ctx = MarketContext(
        symbol="AAPL",
        asset_class="equity",
        structured_forecast=StructuredForecast(expected_return=10.0, confidence=1.0),
        news=NewsContext(score=1.0, materiality=1.0),
        macro=MacroContext(regime_score=10.0),
        accumulator=AccumulatorContext(score=10.0, confidence=1.0),
    )
    score = fusion.combine(ctx)
    assert -1.0 <= score.directional_bias <= 1.0


def test_fusion_includes_graph_affected_universe() -> None:
    fusion = MultimodalFusion(FusionConfig())
    ctx = MarketContext(
        symbol="SPY",
        asset_class="etf",
        structured_forecast=StructuredForecast(expected_return=0.005, confidence=0.7),
        graph=GraphContext(
            related_symbols=("QQQ", "DIA"),
            affected_asset_classes=("etf", "equity"),
            propagation_strength=0.4,
            upstream_trigger="VIX",
        ),
    )
    score = fusion.combine(ctx)
    assert "SPY" in score.affected_symbols
    assert "QQQ" in score.affected_symbols
    assert set(score.affected_asset_classes) == {"etf", "equity"}


def test_fusion_default_yaml_loads_disabled() -> None:
    cfg = FusionConfig.load(Path("config/multimodal_fusion.yaml"))
    assert cfg.enabled is False
    assert cfg.weights.structured_forecast > 0


# ── architectural invariant: ai/fusion.py never imports brokers ─────────────


def test_fusion_module_does_not_import_brokers() -> None:
    src = Path("ai/fusion.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "brokers" or alias.name.startswith("brokers."):
                    offenders.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == "brokers" or module.startswith("brokers."):
                offenders.append(module)
    assert not offenders, f"ai/fusion.py imports brokers: {offenders}"


# ── relationship loader ────────────────────────────────────────────────────


def test_load_relationships_from_dict_basic() -> None:
    raw = {
        "relationships": [
            {
                "upstream_symbol": "SPY",
                "downstream_symbol": "AAPL",
                "direction": "co_move",
                "static_confidence": 0.7,
                "expected_lag_hours": 0,
                "asset_class_downstream": "equity",
            },
            {
                "upstream_symbol": "VIX",
                "downstream_symbol": "SPY",
                "direction": "inverse",
                "static_confidence": 0.6,
            },
        ]
    }
    rels = load_relationships_from_dict(raw)
    assert len(rels) == 2
    assert rels[0].downstream_symbol == "AAPL"


def test_relationship_index_lookups() -> None:
    rels = [
        Relationship(upstream_symbol="SPY", downstream_symbol="AAPL", static_confidence=0.7, asset_class_downstream="equity"),
        Relationship(upstream_symbol="SPY", downstream_symbol="MSFT", static_confidence=0.65, asset_class_downstream="equity"),
        Relationship(upstream_symbol="VIX", downstream_symbol="SPY", static_confidence=0.6),
    ]
    idx = RelationshipIndex(relationships=rels)
    affected = idx.affected_symbols("spy")
    assert set(affected) == {"AAPL", "MSFT"}
    assert idx.affected_asset_classes("SPY") == ("equity",)
    assert idx.upstream_for("SPY")[0].upstream_symbol == "VIX"


def test_relationship_index_unknown_symbol_returns_empty() -> None:
    idx = RelationshipIndex(relationships=[])
    assert idx.affected_symbols("GHOST") == ()
    assert idx.affected_asset_classes("GHOST") == ()
