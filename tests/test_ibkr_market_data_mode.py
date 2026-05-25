"""IBKR market-data mode and snapshot-request hygiene."""

from __future__ import annotations

from decimal import Decimal

import pytest

from brokers.ibkr.adapter import IBKRAdapter, _ibkr_market_data_type_from_env


class _FakeTicker:
    last = 101.25
    close = None
    bid = None
    ask = None


class _FakeIB:
    def __init__(self) -> None:
        self.market_data_types: list[int] = []
        self.req_mkt_data_count = 0
        self.cancel_mkt_data_count = 0

    def isConnected(self) -> bool:  # noqa: N802 - mirrors ib_insync
        return True

    async def qualifyContractsAsync(self, contract):  # noqa: N802 - mirrors ib_insync
        return [contract]

    def reqMarketDataType(self, market_data_type: int) -> None:  # noqa: N802 - mirrors ib_insync
        self.market_data_types.append(market_data_type)

    def reqMktData(self, *_args, **_kwargs) -> None:  # noqa: N802 - mirrors ib_insync
        self.req_mkt_data_count += 1

    def ticker(self, _contract):
        return _FakeTicker()

    def cancelMktData(self, *_args, **_kwargs) -> None:  # noqa: N802 - mirrors ib_insync
        self.cancel_mkt_data_count += 1


def test_ibkr_market_data_type_defaults(monkeypatch) -> None:
    monkeypatch.delenv("IBKR_MARKET_DATA_TYPE", raising=False)

    assert _ibkr_market_data_type_from_env(paper_mode=True) == 3
    assert _ibkr_market_data_type_from_env(paper_mode=False) == 1


def test_ibkr_market_data_type_env_override(monkeypatch) -> None:
    monkeypatch.setenv("IBKR_MARKET_DATA_TYPE", "real-time")
    assert _ibkr_market_data_type_from_env(paper_mode=True) == 1

    monkeypatch.setenv("IBKR_MARKET_DATA_TYPE", "delayed-frozen")
    assert _ibkr_market_data_type_from_env(paper_mode=False) == 4


@pytest.mark.asyncio
async def test_get_last_price_applies_delayed_mode_and_does_not_cancel_snapshot(monkeypatch) -> None:
    monkeypatch.delenv("IBKR_MARKET_DATA_TYPE", raising=False)

    async def _fast_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr("brokers.ibkr.adapter.asyncio.sleep", _fast_sleep)

    adapter = IBKRAdapter(paper_mode=True)
    fake = _FakeIB()
    adapter._ib = fake

    px = await adapter.get_last_price("AAPL")

    assert px == Decimal("101.25")
    assert fake.market_data_types == [3]
    assert fake.req_mkt_data_count == 1
    assert fake.cancel_mkt_data_count == 0

