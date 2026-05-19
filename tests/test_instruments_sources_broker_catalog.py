"""Tests for ``instruments.sources.broker_catalog`` (D116)."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from instruments.sources.base import SourceContext, SourceFetchError
from instruments.sources.broker_catalog import (
    BrokerCatalogSource,
    get_broker_catalog_sources,
)


class _FakeAdapter:
    def __init__(self, symbols: list[str], *, fail: bool = False) -> None:
        self._symbols = symbols
        self._fail = fail

    async def get_supported_symbols(self) -> list[str]:
        if self._fail:
            raise RuntimeError("simulated outage")
        return list(self._symbols)


@pytest.mark.asyncio
async def test_broker_catalog_normalises_per_broker() -> None:
    source = BrokerCatalogSource(broker_name="kraken", adapter=_FakeAdapter(["XBT/USD", "ETH/USD"]))
    result = await source.fetch(SourceContext(started_at=datetime.now(timezone.utc)))
    syms = {c.canonical_symbol for c in result.contributions}
    assert syms == {"BTC-USD", "ETH-USD"}


@pytest.mark.asyncio
async def test_broker_catalog_dedupes_within_batch() -> None:
    source = BrokerCatalogSource(broker_name="alpaca", adapter=_FakeAdapter(["AAPL", "AAPL", "TSLA"]))
    result = await source.fetch(SourceContext(started_at=datetime.now(timezone.utc)))
    assert len(result.contributions) == 2


@pytest.mark.asyncio
async def test_broker_catalog_raises_on_adapter_failure() -> None:
    source = BrokerCatalogSource(broker_name="alpaca", adapter=_FakeAdapter([], fail=True))
    with pytest.raises(SourceFetchError):
        await source.fetch(SourceContext(started_at=datetime.now(timezone.utc)))


def test_get_broker_catalog_sources_respects_exclusion() -> None:
    bm = SimpleNamespace(adapters={"alpaca": _FakeAdapter(["AAPL"]), "ibkr": _FakeAdapter(["SPY"])})
    sources = get_broker_catalog_sources(bm, excluded_brokers=["ibkr"])
    names = {s.broker_name for s in sources}
    assert names == {"alpaca"}


def test_get_broker_catalog_sources_returns_empty_when_no_manager() -> None:
    assert get_broker_catalog_sources(None) == []
    assert get_broker_catalog_sources(SimpleNamespace(adapters={})) == []
