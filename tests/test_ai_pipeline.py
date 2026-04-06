from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from ai.news_classifier import NewsScore
from ai.pipeline import AIPipeline


class _FakeClassifier:
    async def score_batch(self, items):  # noqa: ANN001
        _ = items
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
    scores, details = await p._score_news(symbols=["SPY", "BTC-USD"], rows=rows)
    assert scores["SPY"] > 0
    assert scores["BTC-USD"] < 0
    assert details["SPY"]["event_type"] == "macro"


def test_trend_label():
    from decimal import Decimal

    assert AIPipeline._trend_label([Decimal("5.5"), Decimal("5.0")]) == "up"
    assert AIPipeline._trend_label([Decimal("4.8"), Decimal("5.0")]) == "down"
    assert AIPipeline._trend_label([Decimal("5.01"), Decimal("5.0")]) == "flat"
