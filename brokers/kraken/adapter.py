"""
brokers/kraken/adapter.py
==========================
Kraken adapter — crypto spot and futures.
Primary crypto exchange. 640+ assets, GBP support, UK-friendly.

SDK: pip install python-kraken-sdk
Docs: https://docs.kraken.com/api/
"""

from decimal import Decimal
from typing import AsyncIterator

from brokers.base import (
    BrokerAdapter, Balance, Position, Order, OrderResult,
    Candle, OrderBook, Tick, AssetClass,
)


class KrakenAdapter(BrokerAdapter):
    """
    Adapter for Kraken via python-kraken-sdk.
    Supports: crypto spot (640+ pairs), futures, GBP pairs.
    Fees: 0.25% maker / 0.40% taker at base tier (reduces with volume).
    Paper mode: simulated — Kraken has no native paper trading.
    """

    broker_name = "kraken"

    def __init__(
        self,
        api_key: str = "",
        api_secret: str = "",
        paper_mode: bool = True,
        **kwargs
    ):
        self.api_key = api_key
        self.api_secret = api_secret
        self.paper_mode = paper_mode
        self._client = None         # KrakenSpotWSAPI — initialised on connect()

    async def connect(self) -> bool:
        # TODO M1: implement Kraken WebSocket connection
        # from kraken.spot import SpotWSClient
        # self._client = SpotWSClient(key=self.api_key, secret=self.api_secret)
        raise NotImplementedError("Kraken adapter — implement in M1")

    async def disconnect(self) -> None:
        raise NotImplementedError

    async def is_connected(self) -> bool:
        raise NotImplementedError

    async def get_balance(self) -> list[Balance]:
        raise NotImplementedError

    async def get_positions(self) -> list[Position]:
        raise NotImplementedError

    async def place_order(self, order: Order) -> OrderResult:
        # NOTE: In paper_mode, log the order but don't send it to Kraken
        if self.paper_mode:
            raise NotImplementedError("Paper mode simulation — implement in M1")
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
