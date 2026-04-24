from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from ai.news_classifier import NewsScore
from ai.pipeline import AIPipeline


class _FakeClassifier:
    def __init__(self) -> None:
        self.last_items = []

    async def score_batch(self, items):  # noqa: ANN001
        self.last_items = list(items or [])
        return [
            NewsScore(
                headline="spy up",
                sentiment=0.8,
                confidence=0.9,
                affected_symbols=["SPY"],
                event_type="macro",
                directional_bias="bullish",
                rationale="positive",
                scored_at=datetime.now(timezone.utc).isoformat(),
                decay_hours=24,
            ),
            NewsScore(
                headline="btc down",
                sentiment=0.5,
                confidence=0.8,
                affected_symbols=["BTC-USD"],
                event_type="regulatory",
                directional_bias="bearish",
                rationale="negative",
                scored_at=datetime.now(timezone.utc).isoformat(),
                decay_hours=12,
            ),
        ]


class _ConflictClassifier:
    async def score_batch(self, items):  # noqa: ANN001
        _ = items
        now = datetime.now(timezone.utc).isoformat()
        return [
            NewsScore("a", 0.9, 0.3, ["SPY"], "macro", "bullish", "x", now, 24),
            NewsScore("b", 0.8, 0.2, ["SPY"], "macro", "bearish", "x", now, 24),
            NewsScore("c", 0.7, 0.2, ["SPY"], "macro", "bullish", "x", now, 24),
            NewsScore("d", 0.6, 0.2, ["SPY"], "macro", "bearish", "x", now, 24),
            NewsScore("e", 1.0, 0.3, ["QQQ"], "macro", "bullish", "x", now, 24),
        ]


@pytest.mark.asyncio
async def test_score_news_aggregates_per_symbol():
    p = AIPipeline({}, classifier=_FakeClassifier())
    rows = [
        SimpleNamespace(
            title="a",
            source_name="src",
            published_at=datetime.now(timezone.utc),
            description="d",
        )
    ]
    scores, details, _anomalies = await p._score_news(symbols=["SPY", "BTC-USD"], rows=rows)
    assert scores["SPY"] > 0
    assert scores["BTC-USD"] < 0
    assert details["SPY"]["event_type"] == "macro"


@pytest.mark.asyncio
async def test_score_news_detects_anomalies():
    p = AIPipeline(
        {
            "anomaly_detection": {
                "min_sample_count": 3,
                "disagreement_ratio_threshold": 0.4,
                "high_impact_score_abs": 0.2,
                "low_confidence_threshold": 0.5,
            }
        },
        classifier=_ConflictClassifier(),
    )
    rows = [
        SimpleNamespace(
            title="a",
            source_name="src",
            published_at=datetime.now(timezone.utc),
            description="d",
        )
    ]
    _scores, _details, anomalies = await p._score_news(symbols=["SPY", "QQQ"], rows=rows)
    assert any(a.get("kind") == "conflicting_narrative" for a in anomalies)
    assert any(a.get("kind") == "high_impact_low_confidence" for a in anomalies)


def test_allowed_strategy_names_from_regime_config():
    p = AIPipeline(
        {
            "regime_strategy_gates": {
                "tightening": ["mean_reversion"],
            }
        },
        classifier=_FakeClassifier(),
    )
    assert p.allowed_strategy_names("tightening") == {"mean_reversion"}
    assert p.allowed_strategy_names("neutral") is None


def test_ai_yaml_regime_gates_lists_core_signal_strategies() -> None:
    """config/ai.yaml must list every per-symbol strategy the loop can emit (D032)."""
    from pathlib import Path

    import yaml

    data = yaml.safe_load(Path("config/ai.yaml").read_text(encoding="utf-8"))
    gates = (data.get("pipeline") or {}).get("regime_strategy_gates") or {}
    required = {
        "momentum_breakout",
        "mean_reversion",
        "volume_flow",
        "event_driven_news",
        "pairs_trading",
        "volatility_regime",
        "regime_rotation",
    }
    for regime, names in gates.items():
        assert isinstance(names, list) and names, regime
        assert required.issubset(set(names)), f"{regime} missing strategies: {required - set(names)}"


def test_trend_label():
    from decimal import Decimal

    assert AIPipeline._trend_label([Decimal("5.5"), Decimal("5.0")]) == "up"
    assert AIPipeline._trend_label([Decimal("4.8"), Decimal("5.0")]) == "down"
    assert AIPipeline._trend_label([Decimal("5.01"), Decimal("5.0")]) == "flat"


def test_airouter_runtime_ai_status_degraded_when_no_providers():
    from ai.router import AIRouter

    r = AIRouter({})
    assert r.runtime_ai_status()["kind"] == "local_first"
    assert r.runtime_ai_status()["ai_degraded"] is False

    r._startup_validated = True
    for k in list(r._providers_enabled.keys()):
        r._providers_enabled[k] = False
    assert r.runtime_ai_status()["ai_degraded"] is True


@pytest.mark.asyncio
async def test_score_news_balances_rows_across_sources():
    clf = _FakeClassifier()
    p = AIPipeline({"max_news_items_per_cycle": 3}, classifier=clf)
    now = datetime.now(timezone.utc)
    rows = [
        SimpleNamespace(title="n1", source_name="newsapi", published_at=now, description="d"),
        SimpleNamespace(title="n2", source_name="newsapi", published_at=now, description="d"),
        SimpleNamespace(title="n3", source_name="newsapi", published_at=now, description="d"),
        SimpleNamespace(title="av1", source_name="alphavantage", published_at=now, description="d"),
        SimpleNamespace(title="fh1", source_name="finnhub", published_at=now, description="d"),
    ]
    await p._score_news(symbols=["SPY"], rows=rows)
    used_sources = {str(i.source).lower() for i in clf.last_items}
    assert "newsapi" in used_sources
    assert "alphavantage" in used_sources
    assert "finnhub" in used_sources
