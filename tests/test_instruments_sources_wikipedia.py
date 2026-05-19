"""Tests for the Wikipedia instrument source (D116).

We avoid hitting Wikipedia from CI by patching :func:`polite_get` to
return a small in-memory HTML fixture that mirrors the real S&P 500
table layout. The test ensures parsing + canonicalisation work and that
the recipe-based registry produces real adapters for the static index
entries.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from instruments.sources import wikipedia as wiki_mod
from instruments.sources.base import SourceContext, SourceFetchError
from instruments.sources.wikipedia import (
    WIKIPEDIA_INDEX_REGISTRY,
    WikipediaIndexEntry,
    WikipediaSource,
    get_wikipedia_sources,
)


_FAKE_SP500_HTML = """
<html><body>
<table>
<tr><th>Symbol</th><th>Security</th><th>GICS Sector</th><th>GICS Sub-Industry</th></tr>
<tr><td>AAPL</td><td>Apple Inc.</td><td>Information Technology</td><td>Hardware</td></tr>
<tr><td>MSFT</td><td>Microsoft Corp.</td><td>Information Technology</td><td>Software</td></tr>
<tr><td>BRK.B</td><td>Berkshire Hathaway</td><td>Financials</td><td>Multi-Sector</td></tr>
</table>
</body></html>
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
async def test_wikipedia_source_parses_sp500_fixture(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_polite_get(url: str, **kwargs) -> _FakeResponse:  # noqa: ANN003
        return _FakeResponse(_FAKE_SP500_HTML.encode("utf-8"))

    monkeypatch.setattr(wiki_mod, "polite_get", _fake_polite_get)

    entry = WikipediaIndexEntry(
        source_id="wikipedia.test_sp500",
        url="https://example.invalid/sp500",
        table_index=0,
        symbol_column="Symbol",
        name_column="Security",
        sector_column="GICS Sector",
        industry_column="GICS Sub-Industry",
    )
    source = WikipediaSource(entry)
    ctx = SourceContext(started_at=datetime.now(timezone.utc))
    result = await source.fetch(ctx)
    assert result.source_id == entry.source_id
    by_sym = {c.canonical_symbol: c for c in result.contributions}
    assert "AAPL" in by_sym
    assert by_sym["AAPL"].display_name == "Apple Inc."
    assert by_sym["AAPL"].sector == "Information Technology"
    # Berkshire's dot-class is mapped to BRK-B (yfinance form)
    assert "BRK-B" in by_sym


@pytest.mark.asyncio
async def test_wikipedia_source_raises_when_table_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fake_polite_get(url: str, **kwargs) -> _FakeResponse:  # noqa: ANN003
        return _FakeResponse(b"<html><body><p>No tables here</p></body></html>")

    monkeypatch.setattr(wiki_mod, "polite_get", _fake_polite_get)
    entry = WikipediaIndexEntry(
        source_id="wikipedia.test_missing",
        url="https://example.invalid/missing",
        table_index=0,
        symbol_column="Symbol",
    )
    source = WikipediaSource(entry)
    with pytest.raises(SourceFetchError):
        await source.fetch(SourceContext(started_at=datetime.now(timezone.utc)))


def test_static_registry_contains_expected_indices() -> None:
    ids = {e.source_id for e in WIKIPEDIA_INDEX_REGISTRY}
    assert "wikipedia.sp500" in ids
    assert "wikipedia.ftse100" in ids
    assert "wikipedia.dax40" in ids
    assert "wikipedia.nikkei225" in ids
    assert "wikipedia.asx200" in ids


def test_get_wikipedia_sources_can_filter_by_id() -> None:
    out = get_wikipedia_sources(enabled_source_ids=["wikipedia.sp500", "wikipedia.dax40"])
    assert {s.source_id for s in out} == {"wikipedia.sp500", "wikipedia.dax40"}
