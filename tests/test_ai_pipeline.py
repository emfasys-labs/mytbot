from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
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
                sentiment=-0.5,
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
            NewsScore("b", -0.8, 0.2, ["SPY"], "macro", "bearish", "x", now, 24),
            NewsScore("c", 0.7, 0.2, ["SPY"], "macro", "bullish", "x", now, 24),
            NewsScore("d", -0.6, 0.2, ["SPY"], "macro", "bearish", "x", now, 24),
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
        "trend_breakout",
        "trend_following",
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
async def test_rules_provider_does_not_treat_nasdaq_listing_as_qqq_news():
    from ai.providers.rules_provider import RulesProvider

    r = await RulesProvider().score_headline(
        "Teacher Retirement System of Texas Reduces Stock Position in Insulet Corporation $PODD",
        "Insulet (NASDAQ:PODD) was downgraded by an analyst.",
        "MarketBeat",
        "2026-04-25T11:39:18+01:00",
    )
    assert r.affected_symbols == ["PODD"]


@pytest.mark.asyncio
async def test_rules_provider_still_maps_nasdaq_100_to_qqq():
    from ai.providers.rules_provider import RulesProvider

    r = await RulesProvider().score_headline(
        "Nasdaq 100 futures rally after earnings beats",
        "",
        "Reuters",
        "2026-04-25T11:39:18+01:00",
    )
    assert "QQQ" in r.affected_symbols


@pytest.mark.asyncio
async def test_score_news_skips_low_signal_institutional_filing_rows():
    clf = _FakeClassifier()
    p = AIPipeline({"max_news_items_per_cycle": 3}, classifier=clf)
    now = datetime.now(timezone.utc)
    rows = [
        SimpleNamespace(
            title="Teacher Retirement System of Texas Reduces Stock Position in Insulet Corporation $PODD",
            source_name="MarketBeat",
            published_at=now,
            description="The fund reduced its stake in Q4.",
            url="https://www.marketbeat.com/instant-alerts/filing-x",
        ),
        SimpleNamespace(
            title="Fed signals rate path shift after inflation surprise",
            source_name="Reuters",
            published_at=now,
            description="Central bank officials discussed inflation and rates.",
            url="https://example.test/fed",
        ),
    ]
    await p._score_news(symbols=["SPY"], rows=rows)
    assert [i.headline for i in clf.last_items] == ["Fed signals rate path shift after inflation surprise"]


@pytest.mark.asyncio
async def test_score_news_prefers_tier1_publishers_in_budget():
    clf = _FakeClassifier()
    p = AIPipeline(
        {
            "max_news_items_per_cycle": 3,
            "news_source_selection": {"tier1_min_items": 2, "tier3_max_per_source": 1},
        },
        classifier=clf,
    )
    now = datetime.now(timezone.utc)
    rows = [
        SimpleNamespace(title="agg1", source_name="TradingKey", published_at=now, description="d"),
        SimpleNamespace(title="agg2", source_name="TradingKey", published_at=now, description="d"),
        SimpleNamespace(title="wire1", source_name="Reuters", published_at=now, description="d"),
        SimpleNamespace(title="wire2", source_name="Bloomberg", published_at=now, description="d"),
        SimpleNamespace(title="mid1", source_name="Yahoo Finance", published_at=now, description="d"),
    ]
    await p._score_news(symbols=["SPY"], rows=rows)
    headlines = [i.headline for i in clf.last_items]
    assert "wire1" in headlines
    assert "wire2" in headlines
    assert sum(h.startswith("agg") for h in headlines) <= 1


@pytest.mark.asyncio
async def test_score_news_matches_forex_continuous_suffix_aliases():
    class _FxClassifier:
        async def score_batch(self, items):  # noqa: ANN001
            _ = items
            now = datetime.now(timezone.utc).isoformat()
            return [
                NewsScore(
                    headline="dollar slips",
                    sentiment=-0.4,
                    confidence=0.8,
                    affected_symbols=["GBPUSD=X"],
                    event_type="macro",
                    directional_bias="bearish",
                    rationale="fx move",
                    scored_at=now,
                    decay_hours=24,
                )
            ]

    p = AIPipeline({}, classifier=_FxClassifier())
    rows = [
        SimpleNamespace(
            title="Cable weakens",
            source_name="Reuters",
            published_at=datetime.now(timezone.utc),
            description="d",
        )
    ]
    scores, _details, _anomalies = await p._score_news(symbols=["GBPUSD=X"], rows=rows)
    assert scores["GBPUSD=X"] < 0


def test_news_score_for_symbol_helper():
    from ai.news_scores import news_score_for_symbol

    ai_result = SimpleNamespace(news_scores={"CL=F": 0.25, "SPY": -0.1})
    assert news_score_for_symbol(ai_result, "CL=F") == 0.25
    assert news_score_for_symbol(ai_result, "spy") == -0.1
    assert news_score_for_symbol(ai_result, "QQQ") is None
    assert news_score_for_symbol(None, "SPY") is None


@pytest.mark.asyncio
async def test_local_reasoning_cools_down_after_timeout(monkeypatch):
    from ai.providers.local_reasoning_provider import LocalReasoningProvider

    provider = LocalReasoningProvider(
        {
            "model_name": "gpt-oss:20b",
            "timeout_seconds": 0.01,
            "failure_cooldown_seconds": 60,
        }
    )
    provider._available = True
    provider._active_model = "gpt-oss:20b"
    calls = {"n": 0}

    async def _timeout(*_args, **_kwargs):
        calls["n"] += 1
        raise httpx.ReadTimeout("slow local model")

    monkeypatch.setattr(provider, "_call_llm", _timeout)

    first = await provider.score_headline("Fed decision", None, "Reuters", "2026-06-05T19:00:00Z")
    second = await provider.score_headline("Fed decision", None, "Reuters", "2026-06-05T19:00:00Z")

    assert first.success is False
    assert "slow local model" in (first.error or "")
    assert second.success is False
    assert second.error == "cooldown_after_timeout"
    assert calls["n"] == 1
