"""
brokers/binance/adapter.py
===========================
Binance adapter — highest liquidity crypto exchange.
1000+ trading pairs. 0.10% maker/taker base fee.

SDK: pip install python-binance
Docs: https://python-binance.readthedocs.io/
"""

from decimal import Decimal
from typing import AsyncIterator

from brokers.base import (
    BrokerAdapter, Balance, Position, Order, OrderResult,
    Candle, OrderBook, Tick, AssetClass,
)


class BinanceAdapter(BrokerAdapter):
    broker_name = "binance"

    def __init__(self, api_key: str = "", api_secret: str = "", paper_mode: bool = True, **kwargs):
        self.api_key = api_key
        self.api_secret = api_secret
        self.paper_mode = paper_mode
        self._client = None

    async def connect(self) -> bool:
        raise NotImplementedError("Binance adapter — implement in M1")

    async def disconnect(self) -> None:
        raise NotImplementedError

    async def is_connected(self) -> bool:
        raise NotImplementedError

    async def get_balance(self) -> list[Balance]:
        raise NotImplementedError

    async def get_positions(self) -> list[Position]:
        raise NotImplementedError

    async def place_order(self, order: Order) -> OrderResult:
        raise NotImplementedError

    async def cancel_order(self, broker_order_id: str) -> bool:
        raise NotImplementedError

    async def get_order(self, broker_order_id: str) -> OrderResult:
        raise NotImplementedError

    async def get_open_orders(self) -> list[OrderResult]:
        raise NotImplementedError

    async def get_candles(self, symbol: str, timeframe: str, limit: int = 200) -> list[Candle]:
        raise NotImplementedError

    async def get_order_book(self, symbol: str, depth: int = 10) -> OrderBook:
        raise NotImplementedError

    async def get_last_price(self, symbol: str) -> Decimal:
        raise NotImplementedError

    async def stream_prices(self, symbols: list[str]) -> AsyncIterator[Tick]:
        raise NotImplementedError
        yield

    async def get_supported_symbols(self) -> list[str]:
        raise NotImplementedError

    async def get_asset_class(self, symbol: str) -> AssetClass:
        raise NotImplementedError
