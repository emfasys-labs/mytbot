"""
brokers/ibkr/adapter.py
========================
Interactive Brokers adapter.
Primary broker — stocks, bonds, ETFs, forex, options, futures, crypto (11 coins).

SDK: pip install ib_insync
Docs: https://ib-insync.readthedocs.io/
TWS API: https://interactivebrokers.github.io/tws-api/

Setup:
- Install TWS or IB Gateway on your machine
- Enable API connections in TWS: Edit → Global Config → API → Settings
- Paper trading port: 7497
- Live trading port:  7496
"""

from decimal import Decimal
from typing import AsyncIterator

from brokers.base import (
    BrokerAdapter, Balance, Position, Order, OrderResult,
    Candle, OrderBook, Tick, AssetClass,
    OrderSide, OrderType, OrderStatus
)


class IBKRAdapter(BrokerAdapter):
    """
    Adapter for Interactive Brokers via ib_insync.
    Supports: US/UK/EU equities, ETFs, bonds, forex, options, futures, crypto.
    """

    broker_name = "ibkr"

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 7497,           # 7497 = paper, 7496 = live
        client_id: int = 1,
        account_id: str = "",
        paper_mode: bool = True,
        **kwargs
    ):
        self.host = host
        self.port = port if paper_mode else 7496
        self.client_id = client_id
        self.account_id = account_id
        self.paper_mode = paper_mode
        self._ib = None             # ib_insync.IB() instance — initialised on connect()

    async def connect(self) -> bool:
        # TODO M1: implement ib_insync connection
        # from ib_insync import IB
        # self._ib = IB()
        # await self._ib.connectAsync(self.host, self.port, clientId=self.client_id)
        raise NotImplementedError("IBKR adapter — implement in M1")

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
