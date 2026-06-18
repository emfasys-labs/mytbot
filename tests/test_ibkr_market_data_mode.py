"""IBKR market-data mode and snapshot-request hygiene."""

from __future__ import annotations

from decimal import Decimal

import pytest

from brokers.ibkr.adapter import IBKRAdapter, _ibkr_market_data_type_from_env


class _FakeTicker:
    last = 101.25
    close = None
    bid = 101.20
    ask = 101.30
    bidSize = None
    askSize = None
    domBids: list = []
    domAsks: list = []


class _FakeIB:
    def __init__(self) -> None:
        self.market_data_types: list[int] = []
        self.req_mkt_data_count = 0
        self.cancel_mkt_data_count = 0
        self.req_mkt_depth_count = 0
        self.cancel_mkt_depth_count = 0

    def isConnected(self) -> bool:  # noqa: N802 - mirrors ib_insync
        return True

    async def qualifyContractsAsync(self, contract):  # noqa: N802 - mirrors ib_insync
        return [contract]

    def reqMarketDataType(self, market_data_type: int) -> None:  # noqa: N802 - mirrors ib_insync
        self.market_data_types.append(market_data_type)

    def reqMktData(self, *_args, **_kwargs) -> None:  # noqa: N802 - mirrors ib_insync
        self.req_mkt_data_count += 1

    def reqMktDepth(self, *_args, **_kwargs) -> None:  # noqa: N802 - mirrors ib_insync
        self.req_mkt_depth_count += 1

    def ticker(self, _contract):
        return _FakeTicker()

    def cancelMktData(self, *_args, **_kwargs) -> None:  # noqa: N802 - mirrors ib_insync
        self.cancel_mkt_data_count += 1

    def cancelMktDepth(self, *_args, **_kwargs) -> None:  # noqa: N802 - mirrors ib_insync
        self.cancel_mkt_depth_count += 1


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


@pytest.mark.asyncio
async def test_get_order_book_uses_top_of_book_in_paper_mode(monkeypatch) -> None:
    monkeypatch.delenv("IBKR_MARKET_DATA_TYPE", raising=False)
    monkeypatch.delenv("IBKR_ORDER_BOOK_SOURCE", raising=False)

    async def _fast_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr("brokers.ibkr.adapter.asyncio.sleep", _fast_sleep)

    adapter = IBKRAdapter(paper_mode=True)
    fake = _FakeIB()
    adapter._ib = fake

    ob = await adapter.get_order_book("EURUSD")

    assert ob.bids == [(Decimal("101.2"), Decimal("1000000"))]
    assert ob.asks == [(Decimal("101.3"), Decimal("1000000"))]
    assert fake.req_mkt_depth_count == 0
    assert fake.cancel_mkt_depth_count == 0
    assert fake.req_mkt_data_count == 1
    assert fake.market_data_types == [3]


@pytest.mark.asyncio
async def test_get_order_book_can_force_depth_then_fallback(monkeypatch) -> None:
    monkeypatch.delenv("IBKR_MARKET_DATA_TYPE", raising=False)
    monkeypatch.setenv("IBKR_ORDER_BOOK_SOURCE", "depth")

    async def _fast_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr("brokers.ibkr.adapter.asyncio.sleep", _fast_sleep)

    adapter = IBKRAdapter(paper_mode=True)
    fake = _FakeIB()
    adapter._ib = fake

    ob = await adapter.get_order_book("EURUSD")

    assert ob.bids == [(Decimal("101.2"), Decimal("1000000"))]
    assert ob.asks == [(Decimal("101.3"), Decimal("1000000"))]
    assert fake.req_mkt_depth_count == 1
    assert fake.cancel_mkt_depth_count == 1
    assert fake.req_mkt_data_count == 1
    assert fake.market_data_types == [3]


class _FakeFutTicker:
    last = 74.05
    close = None
    bid = 74.05
    ask = 74.12
    bidSize = 3
    askSize = 3
    domBids: list = []
    domAsks: list = []


class _FakeFutIB(_FakeIB):
    def ticker(self, _contract):  # noqa: D401 - mirrors ib_insync
        return _FakeFutTicker()


@pytest.mark.asyncio
async def test_get_order_book_scales_futures_depth_by_multiplier(monkeypatch) -> None:
    """D165 — IBKR reports futures depth in contracts; the adapter must scale
    sizes to notional-consistent internal units (contracts * multiplier) so
    liquidity/slippage checks compare against ``order.quantity`` correctly."""
    monkeypatch.delenv("IBKR_MARKET_DATA_TYPE", raising=False)
    monkeypatch.delenv("IBKR_ORDER_BOOK_SOURCE", raising=False)

    async def _fast_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr("brokers.ibkr.adapter.asyncio.sleep", _fast_sleep)

    adapter = IBKRAdapter(paper_mode=True)
    adapter._ib = _FakeFutIB()

    ob = await adapter.get_order_book("CL=F")

    # CL multiplier is 1000 → 3 contracts becomes 3000 internal units per side.
    assert ob.bids == [(Decimal("74.05"), Decimal("3000"))]
    assert ob.asks == [(Decimal("74.12"), Decimal("3000"))]


@pytest.mark.asyncio
async def test_get_order_book_does_not_scale_equity(monkeypatch) -> None:
    """Non-futures must be untouched (multiplier 1)."""
    monkeypatch.delenv("IBKR_MARKET_DATA_TYPE", raising=False)
    monkeypatch.delenv("IBKR_ORDER_BOOK_SOURCE", raising=False)

    async def _fast_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr("brokers.ibkr.adapter.asyncio.sleep", _fast_sleep)

    adapter = IBKRAdapter(paper_mode=True)
    adapter._ib = _FakeFutIB()

    ob = await adapter.get_order_book("AAPL")

    assert ob.bids == [(Decimal("74.05"), Decimal("3"))]
    assert ob.asks == [(Decimal("74.12"), Decimal("3"))]
