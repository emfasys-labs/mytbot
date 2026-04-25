from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from api.server import (
    _alias_symbols_for_signal,
    _news_lookup_symbols_for_signals,
    _signal_news_attribution,
    _signal_news_impact_source,
)


def test_signal_news_attribution_picks_nearest_nonzero_rows() -> None:
    t0 = datetime.now(timezone.utc)
    sig = SimpleNamespace(timestamp=t0)
    rows = [
        SimpleNamespace(
            timestamp=t0 - timedelta(minutes=5),
            score=0.42,
            event_type="macro",
            source="local",
            payload={"headline": "Fed hints easing", "provider": "local_llm"},
        ),
        SimpleNamespace(
            timestamp=t0 - timedelta(minutes=2),
            score=0.0,  # ignored
            event_type="macro",
            source="local",
            payload={"headline": "neutral", "provider": "local_llm"},
        ),
        SimpleNamespace(
            timestamp=t0 - timedelta(minutes=15),
            score=-0.33,
            event_type="geopolitics",
            source="local",
            payload={"headline": "Oil supply risk rises", "provider": "rules"},
        ),
    ]
    out = _signal_news_attribution(sig, rows, max_items=2)
    assert len(out) == 2
    assert out[0]["headline"] == "Fed hints easing"
    assert out[0]["source"] == "local_llm"
    assert out[0]["score"] == 0.42


def test_alias_symbols_for_signal_futures_and_fx() -> None:
    assert "SPY" in _alias_symbols_for_signal("ES")
    assert "QQQ" in _alias_symbols_for_signal("NQ")
    assert "GLD" in _alias_symbols_for_signal("GC")
    assert "USD" in _alias_symbols_for_signal("USDJPY")


def test_news_lookup_symbols_include_aliases() -> None:
    syms = _news_lookup_symbols_for_signals(["ES", "BTC-USD"])
    assert "ES" in syms
    assert "SPY" in syms
    assert "BTC-USD" in syms


def test_signal_news_impact_source_distinguishes_accumulator_from_headline() -> None:
    sig = SimpleNamespace(metadata_={"ai_news_score": 0.0, "accumulator_score": 0.47}, news_score=0.47)
    assert _signal_news_impact_source(sig, []) == "accumulator"
    assert _signal_news_impact_source(sig, [{"headline": "Fed hints easing"}]) == "headline"


def test_signal_news_impact_source_uses_direct_ai_news_score() -> None:
    sig = SimpleNamespace(metadata_={"ai_news_score": -0.2, "accumulator_score": 0.0}, news_score=-0.2)
    assert _signal_news_impact_source(sig, []) == "ai_news"

