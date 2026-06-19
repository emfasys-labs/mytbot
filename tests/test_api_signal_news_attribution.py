from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from api.server import (
    _alias_symbols_for_signal,
    _canonical_symbol_for_news_lookup,
    _candidate_news_rows_for_signal,
    _explicit_tickers_in_news_row,
    _is_market_wide_news_row,
    _metadata_float,
    _news_lookup_symbols_for_signals,
    _news_row_matches_logged_symbol,
    _pick_strongest_news_log_per_symbol,
    _signal_news_attribution,
    _signal_news_impact_source,
)
from data.news_quality import is_analyst_research_roundup


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


def test_metadata_float_parses_string_and_decimal_accumulator_scores() -> None:
    md = {"accumulator_score": "0.42", "ai_news_score": "-0"}
    assert _metadata_float(md, "accumulator_score") == pytest.approx(0.42)
    assert _metadata_float(md, "ai_news_score") == 0.0


def test_canonical_news_symbol_strips_router_prefix_for_ai_lookup() -> None:
    assert _canonical_symbol_for_news_lookup("IBKR:AAPL") == "AAPL"
    assert _canonical_symbol_for_news_lookup("kraken:ETH-USD") == "ETH-USD"
    a = _news_lookup_symbols_for_signals(["IBKR:QQQ"])
    b = _news_lookup_symbols_for_signals(["QQQ"])
    assert set(a) == set(b)


def test_signal_news_attribution_accepts_wide_clock_skew_same_day_news() -> None:
    """Regression: attribution used a ±6h / +30min window — missed older AI batches vs newer logs."""
    t0 = datetime(2026, 5, 12, 12, tzinfo=timezone.utc)
    sig = SimpleNamespace(timestamp=t0)
    row_far_before = SimpleNamespace(
        timestamp=t0 - timedelta(hours=30),
        score=0.31,
        event_type="macro",
        source="local",
        payload={"headline": "Older macro note", "provider": "rules"},
    )
    out = _signal_news_attribution(sig, [row_far_before], max_items=1)
    assert len(out) == 1
    assert out[0]["score"] == 0.31

    row_too_far = SimpleNamespace(
        timestamp=t0 - timedelta(hours=60),
        score=0.99,
        event_type="macro",
        source="local",
        payload={"headline": "Stale", "provider": "rules"},
    )
    assert _signal_news_attribution(sig, [row_too_far], max_items=1) == []


def test_market_wide_news_filter_excludes_single_name_earnings() -> None:
    assert not _is_market_wide_news_row(SimpleNamespace(event_type="earnings", payload={"headline": "$PODD reports"}))
    assert _is_market_wide_news_row(SimpleNamespace(event_type="macro", payload={"headline": "Fed cuts rates"}))


def test_market_wide_news_filter_excludes_single_name_even_if_event_is_broad() -> None:
    row = SimpleNamespace(event_type="macro", payload={"headline": "Teacher fund reduces $PODD stock position"})
    assert not _is_market_wide_news_row(row)


def test_market_wide_news_filter_excludes_analyst_roundup() -> None:
    headline = (
        "Here Are Thursday's Best Wall Street Analyst Research Calls: "
        "Albemarle, American Express, CME Group, and More"
    )
    row = SimpleNamespace(event_type="macro", payload={"headline": headline})
    assert not _is_market_wide_news_row(row)


def test_is_analyst_research_roundup_detects_wall_street_list() -> None:
    assert is_analyst_research_roundup(
        "Here Are Thursday's Best Wall Street Analyst Research Calls: Albemarle, American Express"
    )


def test_rules_classifies_analyst_roundup_as_company() -> None:
    from ai.providers.rules_provider import RulesProvider

    import asyncio

    r = asyncio.run(
        RulesProvider().score_headline(
            "Here Are Thursday's Best Wall Street Analyst Research Calls: Albemarle, American Express",
            None,
            "Yahoo Finance",
            "2026-06-18T12:00:00Z",
        )
    )
    assert r.event_type == "company"


def test_direct_candidates_reject_analyst_roundup_even_when_logged_on_symbol() -> None:
    roundup = SimpleNamespace(
        event_type="macro",
        payload={
            "headline": (
                "Here Are Thursday's Best Wall Street Analyst Research Calls: "
                "Albemarle, American Express, CME Group, and More"
            )
        },
    )
    rows = _candidate_news_rows_for_signal("USO", [roundup])
    assert rows == []


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

