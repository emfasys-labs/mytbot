"""Tests for the OpenFIGI secondary-enricher source (D116)."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from instruments.registry import RegistryRow
from instruments.sources import openfigi as openfigi_mod
from instruments.sources.base import SourceContext
from instruments.sources.openfigi import OpenFIGISource


class _FakeResponse:
    def __init__(self, content: bytes) -> None:
        self.content = content
        self.status_code = 200
        self.headers: dict[str, str] = {}
        self.from_cache = False
        self.cached_etag = None
        self.cached_last_modified = None


def _seed_row(symbol: str, *, isin: str | None = None, currency: str = "USD") -> RegistryRow:
    now = datetime.now(timezone.utc)
    return RegistryRow(
        canonical_symbol=symbol,
        display_name=symbol,
        asset_class="equity",
        region="US",
        exchange=None,
        currency=currency,
        sector=None,
        industry=None,
        isin=isin,
        figi=None,
        first_seen_at=now,
        last_seen_at=now,
        last_refreshed_at=now,
        retired_at=None,
    )


@pytest.mark.asyncio
async def test_openfigi_enriches_known_seed(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = [
        {
            "data": [
                {
                    "figi": "BBG000B9XRY4",
                    "compositeFIGI": "BBG000B9XRY4",
                    "name": "APPLE INC",
                    "ticker": "AAPL",
                    "exchCode": "UN",
                    "securityType": "Common Stock",
                    "marketSector": "Equity",
                    "currency": "USD",
                }
            ]
        }
    ]

    async def _fake_polite_get(url: str, **kwargs) -> _FakeResponse:  # noqa: ANN003
        return _FakeResponse(json.dumps(payload).encode("utf-8"))

    monkeypatch.setattr(openfigi_mod, "polite_get", _fake_polite_get)

    source = OpenFIGISource(seed=(_seed_row("AAPL", isin=None),))
    result = await source.fetch(SourceContext(started_at=datetime.now(timezone.utc)))
    assert result.source_id == "openfigi"
    assert len(result.contributions) == 1
    contrib = result.contributions[0]
    assert contrib.canonical_symbol == "AAPL"
    assert contrib.figi == "BBG000B9XRY4"
    assert "openfigi_alternates" in contrib.metadata


@pytest.mark.asyncio
async def test_openfigi_returns_empty_when_no_seed() -> None:
    source = OpenFIGISource(seed=())
    result = await source.fetch(SourceContext(started_at=datetime.now(timezone.utc)))
    assert result.contributions == ()
    assert result.notes == "no seed symbols"


@pytest.mark.asyncio
async def test_openfigi_handles_partial_batch(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = [{"warning": "no match"}]

    async def _fake_polite_get(url: str, **kwargs) -> _FakeResponse:  # noqa: ANN003
        return _FakeResponse(json.dumps(payload).encode("utf-8"))

    monkeypatch.setattr(openfigi_mod, "polite_get", _fake_polite_get)

    source = OpenFIGISource(seed=(_seed_row("ZZZZ"),))
    result = await source.fetch(SourceContext(started_at=datetime.now(timezone.utc)))
    assert result.contributions == []
