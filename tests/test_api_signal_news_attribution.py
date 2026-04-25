from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from api.server import (
    _alias_symbols_for_signal,
    _candidate_news_rows_for_signal,
    _explicit_tickers_in_news_row,
    _is_market_wide_news_row,
    _news_lookup_symbols_for_signals,
    _news_row_matches_logged_symbol,
    _pick_strongest_news_log_per_symbol,
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


def test_market_wide_news_filter_excludes_single_name_earnings() -> None:
    assert not _is_market_wide_news_row(SimpleNamespace(event_type="earnings", payload={"headline": "$PODD reports"}))
    assert _is_market_wide_news_row(SimpleNamespace(event_type="macro", payload={"headline": "Fed cuts rates"}))


def test_market_wide_news_filter_excludes_single_name_even_if_event_is_broad() -> None:
    row = SimpleNamespace(event_type="macro", payload={"headline": "Teacher fund reduces $PODD stock position"})
    assert not _is_market_wide_news_row(row)


def test_direct_candidates_reject_explicit_different_ticker() -> None:
    podd = SimpleNamespace(event_type="earnings", payload={"headline": "Teacher fund reduces $PODD stock position"})
    qqq = SimpleNamespace(event_type="earnings", payload={"headline": "Nasdaq ETF $QQQ rallies"})
    rows = _candidate_news_rows_for_signal("QQQ", [podd, qqq])
    assert rows == [qqq]
    assert _explicit_tickers_in_news_row(podd) == {"PODD"}


def test_logged_ai_news_row_rejects_explicit_symbol_mismatch() -> None:
    bad = SimpleNamespace(symbol="QQQ", payload={"headline": "Teacher fund reduces $PODD stock position"})
    good = SimpleNamespace(symbol="PODD", payload={"headline": "Teacher fund reduces $PODD stock position"})
    assert not _news_row_matches_logged_symbol(bad)
    assert _news_row_matches_logged_symbol(good)


def test_pick_strongest_news_ignores_mismatched_logged_symbol() -> None:
    bad = SimpleNamespace(symbol="QQQ", score=0.9, payload={"headline": "Teacher fund reduces $PODD stock position"})
    good = SimpleNamespace(symbol="PODD", score=0.4, payload={"headline": "Teacher fund reduces $PODD stock position"})
    picked = _pick_strongest_news_log_per_symbol([bad, good])
    assert "QQQ" not in picked
    assert picked["PODD"] is good

