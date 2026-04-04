"""
brokers/_template/adapter.py
=============================
TEMPLATE — Copy this entire file to add a new broker.

Steps:
1. cp -r brokers/_template brokers/newexchange
2. Rename NewExchangeAdapter to match your exchange
3. Implement each method (replace `raise NotImplementedError`)
4. Add to brokers/registry.py: "newexchange": NewExchangeAdapter
5. Done. Nothing else in the system needs to change.

The translate_* methods are helpers to convert between your exchange's
native format and the system's standard data models. Always keep the
translation logic inside the adapter — the rest of the system
should never see exchange-specific data formats.
"""

from decimal import Decimal
from typing import AsyncIterator

from brokers.base import (
    BrokerAdapter, Balance, Position, Order, OrderResult,
    Candle, OrderBook, Tick, AssetClass,
    OrderSide, OrderType, OrderStatus
)


class NewExchangeAdapter(BrokerAdapter):
    """
    Adapter for [Exchange Name].
    API docs: https://docs.newexchange.com/api
    Python SDK: pip install newexchange-python
    """

    broker_name = "newexchange"

    def __init__(self, api_key: str, api_secret: str, paper_mode: bool = True, **kwargs):
        self.api_key = api_key
        self.api_secret = api_secret
        self.paper_mode = paper_mode
        # self.client = NewExchangeClient(api_key, api_secret)

    # ── Account ───────────────────────────────────────────────────────────────

    async def connect(self) -> bool:
        raise NotImplementedError

    async def disconnect(self) -> None:
        raise NotImplementedError

    async def is_connected(self) -> bool:
        raise NotImplementedError

    async def get_balance(self) -> list[Balance]:
        raise NotImplementedError

    async def get_positions(self) -> list[Position]:
        raise NotImplementedError

    # ── Orders ────────────────────────────────────────────────────────────────

    async def place_order(self, order: Order) -> OrderResult:
        raise NotImplementedError

    async def cancel_order(self, broker_order_id: str) -> bool:
        raise NotImplementedError

    async def get_order(self, broker_order_id: str) -> OrderResult:
        raise NotImplementedError

    async def get_open_orders(self) -> list[OrderResult]:
        raise NotImplementedError

    # ── Market Data ───────────────────────────────────────────────────────────

    async def get_candles(self, symbol: str, timeframe: str, limit: int = 200) -> list[Candle]:
        raise NotImplementedError

    async def get_order_book(self, symbol: str, depth: int = 10) -> OrderBook:
        raise NotImplementedError

    async def get_last_price(self, symbol: str) -> Decimal:
        raise NotImplementedError

    async def stream_prices(self, symbols: list[str]) -> AsyncIterator[Tick]:
        raise NotImplementedError
        yield  # makes this a generator

    # ── Broker Info ───────────────────────────────────────────────────────────

    async def get_supported_symbols(self) -> list[str]:
        raise NotImplementedError

    async def get_asset_class(self, symbol: str) -> AssetClass:
        raise NotImplementedError

    # ── Private helpers ───────────────────────────────────────────────────────
    # Keep all exchange-specific translation logic here.
    # The rest of the system never sees raw exchange responses.

    def _translate_order(self, order: Order) -> dict:
        """Convert standard Order → exchange-native order dict."""
        raise NotImplementedError

    def _translate_result(self, raw: dict) -> OrderResult:
        """Convert exchange-native response → standard OrderResult."""
        raise NotImplementedError

    def _translate_candle(self, raw: dict, symbol: str, timeframe: str) -> Candle:
        """Convert exchange-native OHLCV → standard Candle."""
        raise NotImplementedError
