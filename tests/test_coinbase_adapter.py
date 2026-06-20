from __future__ import annotations

from decimal import Decimal

from types import SimpleNamespace

import pytest

from brokers.base import Order, OrderSide, OrderStatus, OrderType
from brokers.coinbase.adapter import CoinbaseAdapter, _canonical_product, _product_id
from brokers.coinbase.auth import build_rest_jwt, normalize_pem_secret


def test_product_id_helpers() -> None:
    assert _product_id("BTC-USD") == "BTC-USD"
    assert _product_id("BTCUSDT") == "BTC-USD"
    assert _canonical_product("ETH-USD") == "ETH-USD"


def test_normalize_pem_secret_unescapes_newlines() -> None:
    raw = "-----BEGIN EC PRIVATE KEY-----\\nABC\\n-----END EC PRIVATE KEY-----\\n"
    assert "\nABC\n" in normalize_pem_secret(raw)


def test_build_rest_jwt_es256() -> None:
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives import serialization

    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    api_key = "organizations/test/apiKeys/key-id"
    token = build_rest_jwt(api_key, pem, "GET", "/api/v3/brokerage/accounts")
    assert isinstance(token, str)
    assert len(token.split(".")) == 3


@pytest.mark.asyncio
async def test_connect_lists_accounts() -> None:
    adapter = CoinbaseAdapter(api_key="organizations/x/apiKeys/y", api_secret="dummy")
    adapter._client = SimpleNamespace()

    async def fake_request(method: str, rel: str, **kwargs):  # noqa: ANN003
        _ = (method, kwargs)
        if rel == "/accounts":
            return {"accounts": [{"currency": "USD", "available_balance": {"value": "100", "currency": "USD"}}]}
        if rel == "/market/products":
            return {"products": [{"product_id": "BTC-USD"}]}
        return {}

    adapter._request = fake_request  # type: ignore[method-assign]
    ok = await adapter.connect()
    assert ok is True
    assert adapter._private_ok is True


@pytest.mark.asyncio
async def test_paper_mode_rejects_live_order() -> None:
    adapter = CoinbaseAdapter(api_key="k", api_secret="s", paper_mode=True)
    adapter._connected = True
    adapter._private_ok = True
    order = Order(
        symbol="BTC-USD",
        side=OrderSide.BUY,
        quantity=Decimal("0.01"),
        order_type=OrderType.MARKET,
    )
    result = await adapter.place_order(order)
    assert result.status == OrderStatus.REJECTED
