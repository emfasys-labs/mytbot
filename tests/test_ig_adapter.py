from __future__ import annotations

from decimal import Decimal

from types import SimpleNamespace

import pytest

from brokers.base import Order, OrderSide, OrderStatus, OrderType
from brokers.ig.adapter import (
    IGAdapter,
    _epic_to_canonical,
    _guess_search_term,
)


def test_epic_symbol_helpers() -> None:
    assert _guess_search_term("AAPL") == "AAPL"
    assert _guess_search_term("EURUSD=X") == "EURUSD"
    assert _guess_search_term("ES=F") == "US500"
    assert _epic_to_canonical("CS.D.EURUSD.CFD.IP") == "EURUSD=X"


@pytest.mark.asyncio
async def test_create_session_stores_tokens() -> None:
    adapter = IGAdapter(
        api_key="key",
        password="secret",
        identifier="ig-user",
        paper_mode=True,
    )
    adapter._client = SimpleNamespace()

    class _Resp:
        status_code = 200
        content = b'{"currentAccountId":"PZVI2","currency":"GBP"}'

        def json(self) -> dict:
            return {"currentAccountId": "PZVI2", "currency": "GBP"}

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
    assert adapter._account_id == "PZVI2"


@pytest.mark.asyncio
async def test_place_market_order_posts_position() -> None:
    adapter = IGAdapter(
        api_key="k",
        password="s",
        identifier="ig-user",
    )
    adapter._connected = True
    adapter._client = object()
    adapter._cst = "cst"
    adapter._security_token = "sec"
    adapter._epic_by_canonical["AAPL"] = "UA.D.AAPL.CFD.IP"
    captured: list[tuple[str, str]] = []

    async def fake_request(method: str, path: str, **kwargs):  # noqa: ANN003
        captured.append((method, path))
        if path == "/positions/otc":
            return {"dealReference": "o_test"}
        if path.startswith("/confirms/"):
            return {"dealStatus": "ACCEPTED", "affectedDeals": [{"dealId": "d1", "level": 150}]}
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
    assert ("POST", "/positions/otc") in captured
    assert result.status == OrderStatus.FILLED
    assert result.broker_order_id == "d1"


@pytest.mark.asyncio
async def test_get_positions_maps_epic() -> None:
    adapter = IGAdapter(
        api_key="k",
        password="s",
        identifier="ig-user",
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
                            "epic": "UA.D.AAPL.CFD.IP",
                            "instrumentName": "Apple Inc",
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
    assert positions[0].broker == "ig"
