"""Tests for the iShares ETF holdings instrument source (D116)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from instruments.sources import ishares as ishares_mod
from instruments.sources.base import SourceContext, SourceFetchError
from instruments.sources.ishares import (
    ISHARES_FUND_REGISTRY,
    IsharesFundEntry,
    IsharesSource,
    get_ishares_sources,
)


_FAKE_IVV_CSV = """
"iShares Core S&P 500 ETF"
"Fund Holdings as of",13-May-2026

Ticker,Name,Sector,Asset Class,Market Value,Weight (%),ISIN
AAPL,APPLE INC,Information Technology,Equity,1500000000,7.20,US0378331005
MSFT,MICROSOFT CORP,Information Technology,Equity,1400000000,6.75,US5949181045
BRK.B,BERKSHIRE HATHAWAY,Financials,Equity,500000000,2.40,US0846707026
USD,USD CURRENCY,-,Cash,1000000,0.005,-
""".strip()


class _FakeResponse:
    def __init__(self, content: bytes) -> None:
        self.content = content
        self.status_code = 200
        self.headers: dict[str, str] = {}
        self.from_cache = False
        self.cached_etag = None
        self.cached_last_modified = None


@pytest.mark.asyncio
async def test_ishares_parses_holdings_and_filters_cash(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_polite_get(url: str, **kwargs) -> _FakeResponse:  # noqa: ANN003
        return _FakeResponse(_FAKE_IVV_CSV.encode("utf-8"))

    monkeypatch.setattr(ishares_mod, "polite_get", _fake_polite_get)

    entry = IsharesFundEntry(
        source_id="ishares.IVV.test",
        fund_ticker="IVV",
        holdings_url="https://example.invalid/IVV/holdings.csv",
        asset_class="equity",
        region="US",
    )
    source = IsharesSource(entry)
    result = await source.fetch(SourceContext(started_at=datetime.now(timezone.utc)))
    by_sym = {c.canonical_symbol: c for c in result.contributions}
    assert "AAPL" in by_sym
    assert "MSFT" in by_sym
    # Berkshire's dot-class converts to yfinance hyphen form
    assert "BRK-B" in by_sym
    # 'USD' currency row with asset_class=Cash must be filtered out
    assert all(c.canonical_symbol != "USD" for c in result.contributions)
    aapl = by_sym["AAPL"]
    assert aapl.isin == "US0378331005"
    assert aapl.sector == "Information Technology"


@pytest.mark.asyncio
async def test_ishares_raises_on_missing_header(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_polite_get(url: str, **kwargs) -> _FakeResponse:  # noqa: ANN003
        return _FakeResponse(b"random,csv,without,header,marker\nfoo,bar,baz,qux,quux\n")

    monkeypatch.setattr(ishares_mod, "polite_get", _fake_polite_get)
    entry = IsharesFundEntry(
        source_id="ishares.broken",
        fund_ticker="XXX",
        holdings_url="https://example.invalid/broken.csv",
        asset_class="equity",
        region="US",
    )
    source = IsharesSource(entry)
    with pytest.raises(SourceFetchError):
        await source.fetch(SourceContext(started_at=datetime.now(timezone.utc)))


def test_registry_contains_expected_funds() -> None:
    ids = {e.source_id for e in ISHARES_FUND_REGISTRY}
    assert "ishares.IVV" in ids
    assert "ishares.IWM" in ids
    assert "ishares.AGG" in ids
    assert "ishares.TLT" in ids


def test_get_ishares_sources_filter() -> None:
    out = get_ishares_sources(enabled_source_ids=["ishares.IVV", "ishares.TLT"])
    assert {s.source_id for s in out} == {"ishares.IVV", "ishares.TLT"}
