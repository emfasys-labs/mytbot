from __future__ import annotations

from decimal import Decimal

import pytest

from brokers.base import Order, OrderSide, OrderType
from brokers.trading212.adapter import (
    Trading212Adapter,
    _guess_t212_ticker,
    _ticker_to_canonical,
)


def test_ticker_symbol_helpers() -> None:
    assert _ticker_to_canonical("AAPL_US_EQ") == "AAPL"
    assert _guess_t212_ticker("AAPL") == "AAPL_US_EQ"
    assert _guess_t212_ticker("HSBA.L") == "HSBA_GB_EQ"


@pytest.mark.asyncio
async def test_load_instruments_builds_symbol_map() -> None:
    adapter = Trading212Adapter(api_key="key", api_secret="secret", paper_mode=True)
    adapter._client = object()

    async def fake_request(method: str, path: str, **kwargs):  # noqa: ANN003
        _ = (method, kwargs)
        return [{"ticker": "AAPL_US_EQ", "shortName": "Apple"}]

    adapter._request = fake_request  # type: ignore[method-assign]
    await adapter._load_instruments()
    assert adapter._ticker_by_canonical["AAPL"] == "AAPL_US_EQ"
    assert adapter._canonical_by_ticker["AAPL_US_EQ"] == "AAPL"


@pytest.mark.asyncio
async def test_place_market_order_posts_signed_quantity() -> None:
    adapter = Trading212Adapter(api_key="k", api_secret="s", paper_mode=True)
    adapter._connected = True
    adapter._client = object()
    adapter._ticker_by_canonical["AAPL"] = "AAPL_US_EQ"
    captured: dict = {}

    async def fake_request(method: str, path: str, **kwargs):  # noqa: ANN003
        captured["method"] = method
        captured["path"] = path
        captured["json"] = kwargs.get("json_body")
        return {"id": 42, "ticker": "AAPL_US_EQ", "quantity": -2, "status": "FILLED"}

    adapter._request = fake_request  # type: ignore[method-assign]

    order = Order(
        symbol="AAPL",
        side=OrderSide.SELL,
        quantity=Decimal("2"),
        order_type=OrderType.MARKET,
    )
    result = await adapter.place_order(order)
    assert captured["path"] == "/equity/orders/market"
    assert captured["json"]["ticker"] == "AAPL_US_EQ"
    assert captured["json"]["quantity"] == -2.0
    assert result.broker_order_id == "42"
    assert result.side == OrderSide.SELL


@pytest.mark.asyncio
async def test_get_positions_maps_tickers() -> None:
    adapter = Trading212Adapter(api_key="k", api_secret="s")
    adapter._connected = True
    adapter._client = object()

    async def fake_request(method: str, path: str, **kwargs):  # noqa: ANN003
        _ = (method, kwargs)
        if path == "/equity/positions":
            return [
                {
                    "ticker": "AAPL_US_EQ",
                    "quantity": 3,
                    "averagePrice": 100,
                    "currentPrice": 110,
                    "ppl": 30,
                }
            ]
        return []

    adapter._request = fake_request  # type: ignore[method-assign]
    positions = await adapter.get_positions()
    assert len(positions) == 1
    assert positions[0].symbol == "AAPL"
    assert positions[0].quantity == Decimal("3")
    assert positions[0].broker == "trading212"


@pytest.mark.asyncio
async def test_get_balance_reuses_endpoint_specific_cache() -> None:
    adapter = Trading212Adapter(api_key="k", api_secret="s")
    adapter._connected = True
    adapter._client = object()
    calls = 0

    async def fake_request(method: str, path: str, **kwargs):  # noqa: ANN003
        nonlocal calls
        _ = (method, path, kwargs)
        calls += 1
        return {
            "currency": "GBP",
            "totalValue": 1250,
            "cash": {"availableToTrade": 1000},
        }

    adapter._request = fake_request  # type: ignore[method-assign]
    first = await adapter.get_balance()
    second = await adapter.get_balance()

    assert calls == 1
    assert first == second
    assert first[0].total == Decimal("1250")
