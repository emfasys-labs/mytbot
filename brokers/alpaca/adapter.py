"""
brokers/alpaca/adapter.py
==========================
Alpaca adapter — US equities, ETFs, crypto. Best paper trading environment.
Zero commission on US stocks. Best for early paper trading while IBKR is being set up.

SDK: pip install alpaca-py
Docs: https://docs.alpaca.markets/
Paper base URL: https://paper-api.alpaca.markets
Live base URL:  https://api.alpaca.markets
"""

from decimal import Decimal
from typing import AsyncIterator

from brokers.base import (
    BrokerAdapter, Balance, Position, Order, OrderResult,
    Candle, OrderBook, Tick, AssetClass,
)


class AlpacaAdapter(BrokerAdapter):
    broker_name = "alpaca"

    PAPER_URL = "https://paper-api.alpaca.markets"
    LIVE_URL  = "https://api.alpaca.markets"

    def __init__(self, api_key: str = "", api_secret: str = "", paper_mode: bool = True, **kwargs):
        self.api_key = api_key
        self.api_secret = api_secret
        self.paper_mode = paper_mode
        self.base_url = self.PAPER_URL if paper_mode else self.LIVE_URL
        self._client = None

    async def connect(self) -> bool:
        raise NotImplementedError("Alpaca adapter — implement in M1")

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
