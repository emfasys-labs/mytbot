from __future__ import annotations

from decimal import Decimal

from types import SimpleNamespace

import pytest

from brokers.base import Order, OrderSide, OrderStatus, OrderType
from brokers.oanda.adapter import (
    OandaAdapter,
    _canonical_instrument,
    _instrument_id,
    resolve_oanda_credentials,
    resolve_oanda_paper_mode,
)


def test_instrument_helpers() -> None:
    assert _instrument_id("EURUSD=X") == "EUR_USD"
    assert _instrument_id("GBPUSD") == "GBP_USD"
    assert _canonical_instrument("EUR_USD") == "EURUSD=X"


def test_resolve_oanda_credentials_picks_practice_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OANDA_API_TOKEN", "live-token")
    monkeypatch.setenv("OANDA_API_TOKEN_PAPER", "practice-token")
    monkeypatch.setenv("OANDA_PAPER_MODE", "true")
    paper, token, _ = resolve_oanda_credentials(paper_mode=False)
    assert paper is True
    assert token == "practice-token"


def test_resolve_oanda_credentials_picks_live_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OANDA_API_TOKEN", "live-token")
    monkeypatch.setenv("OANDA_API_TOKEN_PAPER", "practice-token")
    monkeypatch.setenv("OANDA_PAPER_MODE", "false")
    paper, token, _ = resolve_oanda_credentials(paper_mode=True)
    assert paper is False
    assert token == "live-token"


def test_resolve_oanda_credentials_never_falls_back_across_environments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OANDA_API_TOKEN", "live-token")
    monkeypatch.delenv("OANDA_API_TOKEN_PAPER", raising=False)
    monkeypatch.setenv("OANDA_PAPER_MODE", "true")
    paper, token, account = resolve_oanda_credentials(
        paper_mode=True,
        account_id="live-account",
        account_id_paper="",
    )
    assert paper is True
    assert token == ""
    assert account == ""


def test_resolve_oanda_paper_mode_follows_app_env_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OANDA_PAPER_MODE", raising=False)
    assert resolve_oanda_paper_mode(paper_mode=True) is True
    assert resolve_oanda_paper_mode(paper_mode=False) is False


@pytest.mark.asyncio
async def test_connect_discovers_account() -> None:
    adapter = OandaAdapter(api_token_paper="token", paper_mode=True)
    adapter._client = SimpleNamespace()

    async def fake_request(method: str, path: str, **kwargs):  # noqa: ANN003
        _ = (method, kwargs)
        if path == "/accounts":
            return {"accounts": [{"id": "101-001-1-001", "tags": []}]}
        if path.endswith("/summary"):
            return {"account": {"NAV": "10000", "marginAvailable": "10000", "currency": "USD"}}
        if path.endswith("/instruments"):
            return {"instruments": [{"name": "EUR_USD"}, {"name": "GBP_USD"}]}
        return {}

    adapter._request = fake_request  # type: ignore[method-assign]
    ok = await adapter.connect()
    assert ok is True
    assert adapter.account_id == "101-001-1-001"
    assert adapter._private_ok is True


@pytest.mark.asyncio
async def test_place_market_order_units_sign() -> None:
    adapter = OandaAdapter(
        api_token_paper="t",
        account_id_paper="101-001-1-001",
    )
    adapter._connected = True
    adapter._private_ok = True
    adapter._client = object()
    captured: dict[str, object] = {}

    async def fake_request(method: str, path: str, **kwargs):  # noqa: ANN003
        if method == "POST" and path.endswith("/orders"):
            captured["body"] = kwargs.get("json_body")
            return {
                "orderCreateTransaction": {"id": "1"},
                "orderFillTransaction": {"id": "2", "price": "1.0850", "units": "1000"},
            }
        return {}

    adapter._request = fake_request  # type: ignore[method-assign]
    order = Order(
        symbol="EURUSD=X",
        side=OrderSide.SELL,
        quantity=Decimal("1000"),
        order_type=OrderType.MARKET,
    )
    result = await adapter.place_order(order)
    assert result.status == OrderStatus.FILLED
    body = captured.get("body")
    assert isinstance(body, dict)
    assert body["order"]["units"] == "-1000"


@pytest.mark.asyncio
async def test_missing_token_fails_connect() -> None:
    adapter = OandaAdapter(api_token="")
    ok = await adapter.connect()
    assert ok is False
