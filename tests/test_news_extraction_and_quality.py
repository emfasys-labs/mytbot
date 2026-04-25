"""
Regression tests for ticker extraction false-positives and the
low-signal news filter. Covers the PODD->QQQ class of bug and
the Teacher Retirement System filing noise.

See data/news_quality.py and ai/providers/rules_provider.py.
"""

from __future__ import annotations

from data.news_quality import (
    is_displayable_news_item,
    is_low_signal_institutional_filing,
)


# ── ticker extraction ──────────────────────────────────────────────────────


def _extract(text: str) -> list[str]:
    from ai.providers.rules_provider import RulesProvider

    return RulesProvider()._extract_tickers(text)


def test_bare_nasdaq_listing_does_not_yield_qqq():
    # Was the original PODD bug: "(NASDAQ:PODD)" -> ["PODD", "QQQ"]
    syms = _extract("Insulet Corporation (NASDAQ:PODD) Q1 earnings beat estimates")
    assert "PODD" in syms
    assert "QQQ" not in syms


def test_nasdaq_index_phrasing_still_yields_qqq():
    assert "QQQ" in _extract("Nasdaq 100 climbs to record high")
    assert "QQQ" in _extract("The Nasdaq Composite slipped 1.2%")
    assert "QQQ" in _extract("NASDAQ-100 futures rebound overnight")


def test_goldman_sachs_does_not_yield_gold_etf():
    syms = _extract("Goldman Sachs upgrades semiconductor sector")
    assert "GS" in syms
    assert "GLD" not in syms


def test_oil_substring_does_not_match_inside_other_words():
    assert "USO" not in _extract("Workers toil through long shifts at the plant")
    assert "USO" not in _extract("New boiler regulations announced today")


def test_oil_as_word_still_matches():
    assert "USO" in _extract("Crude oil prices spike on OPEC decision")


def test_dollar_ticker_still_extracted():
    assert "PODD" in _extract("$PODD jumps after FDA clearance")


# ── low-signal filing filter ───────────────────────────────────────────────


def test_filter_drops_teacher_retirement_holdings_update():
    assert is_low_signal_institutional_filing(
        "Teacher Retirement System of Texas Acquires Shares of $PODD"
    )


def test_filter_drops_form_4():
    # Codex's filter relies on holding-term + institutional-name; this
    # test covers the explicit form-number pattern path.
    assert is_low_signal_institutional_filing("CFO files Form 4 disclosing share sale")


def test_filter_drops_13d_schedule():
    assert is_low_signal_institutional_filing("Activist files Schedule 13D on company")


def test_filter_keeps_genuine_earnings():
    assert not is_low_signal_institutional_filing(
        "Apple reports record Q1 revenue, beats estimates"
    )


def test_filter_keeps_macro():
    assert not is_low_signal_institutional_filing(
        "Fed signals rate cut at next FOMC meeting"
    )


def test_filter_keeps_mna():
    assert not is_low_signal_institutional_filing(
        "Microsoft to acquire Activision in $69B deal"
    )


class _FakeRow:
    def __init__(self, title: str, description: str = "", source_name: str = "", url: str = ""):
        self.title = title
        self.description = description
        self.source_name = source_name
        self.url = url


def test_is_displayable_news_item_filters_filings():
    assert not is_displayable_news_item(
        _FakeRow("Teacher Retirement System acquires shares of XYZ")
    )
    assert is_displayable_news_item(_FakeRow("Apple reports record Q1 revenue"))
