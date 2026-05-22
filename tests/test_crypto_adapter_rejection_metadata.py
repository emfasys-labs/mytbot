from decimal import Decimal

import pytest

from brokers.base import Order, OrderSide, OrderStatus, OrderType
from brokers.binance.adapter import BinanceAdapter, _binance_symbol
from brokers.bybit.adapter import BybitAdapter, _bybit_symbol
from brokers.kraken.adapter import KrakenAdapter


def _order(symbol: str) -> Order:
    return Order(
        symbol=symbol,
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Decimal("0.01"),
        limit_price=Decimal("100"),
        client_order_id="test-paper-reject",
    )


def test_usd_crypto_canonical_symbols_map_to_usdt_venue_books() -> None:
    assert _binance_symbol("BTC-USD") == "BTCUSDT"
    assert _binance_symbol("ETH/USD") == "ETHUSDT"
    assert _bybit_symbol("BTC-USD") == "BTCUSDT"
    assert _bybit_symbol("ETH/USD") == "ETHUSDT"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("adapter", "symbol", "broker_name"),
    [
        (KrakenAdapter(paper_mode=True), "BTC/USD", "kraken"),
        (BinanceAdapter(paper_mode=True), "BTC/USDT", "binance"),
        (BybitAdapter(paper_mode=True), "BTC/USDT", "bybit"),
    ],
)
async def test_crypto_paper_rejections_are_explained(adapter, symbol, broker_name):
    order = _order(symbol)

    result = await adapter.place_order(order)

    assert result.status == OrderStatus.REJECTED
    assert order.instrument_metadata["reject_reason"] == "paper_mode_no_native_order"
    assert order.instrument_metadata["rejected_by"] == broker_name
    assert order.instrument_metadata["error_message"]
