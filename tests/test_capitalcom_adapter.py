from __future__ import annotations

from decimal import Decimal

from types import SimpleNamespace

import pytest

from brokers.base import Order, OrderSide, OrderStatus, OrderType
from brokers.capitalcom.adapter import (
    CapitalComAdapter,
    _epic_to_canonical,
    _guess_epic,
)


def test_epic_symbol_helpers() -> None:
    assert _guess_epic("AAPL") == "AAPL"
    assert _guess_epic("EURUSD=X") == "EURUSD"
    assert _guess_epic("ES=F") == "US500"
    assert _epic_to_canonical("EURUSD") == "EURUSD=X"


@pytest.mark.asyncio
async def test_create_session_stores_tokens() -> None:
    adapter = CapitalComAdapter(
        api_key="key",
        api_password="secret",
        identifier="user@example.com",
        paper_mode=True,
    )
    adapter._client = SimpleNamespace()

    class _Resp:
        status_code = 200
        content = b'{"currency":"USD"}'

        def json(self) -> dict:
            return {"currency": "USD"}

        @property
        def headers(self) -> dict[str, str]:
            return {"CST": "cst-token", "X-SECURITY-TOKEN": "sec-token"}

        @property
        def request(self) -> object:
            return object()

    async def fake_post(url: str, **kwargs):  # noqa: ANN003
        _ = (url, kwargs)
        return _Resp()

    adapter._client.post = fake_post  # type: ignore[attr-defined, union-attr]
    await adapter._create_session()
    assert adapter._cst == "cst-token"
    assert adapter._security_token == "sec-token"


@pytest.mark.asyncio
async def test_place_market_order_posts_position() -> None:
    adapter = CapitalComAdapter(
        api_key="k",
        api_password="s",
        identifier="user@example.com",
    )
    adapter._connected = True
    adapter._client = object()
    adapter._cst = "cst"
    adapter._security_token = "sec"
    adapter._epic_by_canonical["AAPL"] = "AAPL"
    captured: list[tuple[str, str]] = []

    async def fake_request(method: str, path: str, **kwargs):  # noqa: ANN003
        captured.append((method, path))
        if path == "/positions":
            return {"dealReference": "o_test"}
        if path.startswith("/confirms/"):
            return {"status": "ACCEPTED", "affectedDeals": [{"dealId": "d1", "level": 150}]}
        return {}

    adapter._request = fake_request  # type: ignore[method-assign]
    async def _noop_session(**_: object) -> None:
        return None

    adapter._ensure_session = _noop_session  # type: ignore[method-assign]

    order = Order(
        symbol="AAPL",
        side=OrderSide.BUY,
        quantity=Decimal("2"),
        order_type=OrderType.MARKET,
    )
    result = await adapter.place_order(order)
    assert ("POST", "/positions") in captured
    assert result.status == OrderStatus.FILLED
    assert result.broker_order_id == "d1"


@pytest.mark.asyncio
async def test_get_positions_maps_epic() -> None:
    adapter = CapitalComAdapter(
        api_key="k",
        api_password="s",
        identifier="user@example.com",
    )
    adapter._connected = True
    adapter._client = object()
    adapter._cst = "cst"
    adapter._security_token = "sec"

    async def fake_request(method: str, path: str, **kwargs):  # noqa: ANN003
        _ = (method, kwargs)
        if path == "/positions":
            return {
                "positions": [
                    {
                        "position": {
                            "dealId": "deal-1",
                            "size": 3,
                            "direction": "BUY",
                            "level": 100,
                            "upl": 5,
                        },
                        "market": {
                            "epic": "AAPL",
                            "symbol": "AAPL",
                            "instrumentType": "SHARES",
                            "bid": 101,
                            "offer": 102,
                        },
                    }
                ]
            }
        return {}

    adapter._request = fake_request  # type: ignore[method-assign]
    positions = await adapter.get_positions()
    assert len(positions) == 1
    assert positions[0].symbol == "AAPL"
    assert positions[0].broker == "capitalcom"
