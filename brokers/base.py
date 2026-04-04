"""
brokers/base.py
===============
The single most important file in this codebase.

Every broker — IBKR, Kraken, Binance, Bybit, Deribit, anything added in future —
must implement this interface. The rest of the system never knows which broker
it's talking to. It only speaks this interface.

Adding a new broker = one new folder + one new file implementing these methods.
Zero changes to anything else.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import AsyncIterator, Optional


# ─── Enums ────────────────────────────────────────────────────────────────────

class OrderSide(Enum):
    BUY = "buy"
    SELL = "sell"


class OrderType(Enum):
    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"
    STOP_LIMIT = "stop_limit"


class OrderStatus(Enum):
    PENDING = "pending"
    OPEN = "open"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


class AssetClass(Enum):
    EQUITY = "equity"
    CRYPTO = "crypto"
    BOND = "bond"
    FOREX = "forex"
    ETF = "etf"
    FUTURE = "future"
    OPTION = "option"


# ─── Data Models ──────────────────────────────────────────────────────────────

@dataclass
class Balance:
    currency: str
    total: Decimal
    available: Decimal
    reserved: Decimal


@dataclass
class Position:
    symbol: str
    asset_class: AssetClass
    quantity: Decimal
    avg_entry_price: Decimal
    current_price: Decimal
    unrealised_pnl: Decimal
    broker: str


@dataclass
class Order:
    symbol: str
    side: OrderSide
    order_type: OrderType
    quantity: Decimal
    limit_price: Optional[Decimal] = None
    stop_price: Optional[Decimal] = None
    client_order_id: Optional[str] = None   # idempotency key
    time_in_force: str = "GTC"


@dataclass
class OrderResult:
    broker_order_id: str
    client_order_id: Optional[str]
    status: OrderStatus
    symbol: str
    side: OrderSide
    quantity: Decimal
    filled_quantity: Decimal
    avg_fill_price: Optional[Decimal]
    fee: Optional[Decimal]
    timestamp: str


@dataclass
class Candle:
    symbol: str
    timestamp: str
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    timeframe: str  # "1m", "5m", "1h", "1d"


@dataclass
class OrderBook:
    symbol: str
    timestamp: str
    bids: list[tuple[Decimal, Decimal]]   # [(price, size), ...]
    asks: list[tuple[Decimal, Decimal]]


@dataclass
class Tick:
    symbol: str
    timestamp: str
    price: Decimal
    volume: Decimal
    bid: Optional[Decimal]
    ask: Optional[Decimal]


# ─── The Interface ────────────────────────────────────────────────────────────

class BrokerAdapter(ABC):
    """
    Abstract base class for all broker/exchange adapters.

    Every concrete adapter (IBKR, Kraken, Binance, Bybit, etc.) must
    implement all abstract methods below. The interface is frozen — it
    never changes. New brokers adapt to it, not the other way around.
    """

    broker_name: str = "base"
    paper_mode: bool = True

    # ── Account ───────────────────────────────────────────────────────────────

    @abstractmethod
    async def connect(self) -> bool:
        """Establish connection to broker API. Return True if successful."""
        ...

    @abstractmethod
    async def disconnect(self) -> None:
        """Clean disconnect from broker API."""
        ...

    @abstractmethod
    async def is_connected(self) -> bool:
        """Return True if currently connected and API is healthy."""
        ...

    @abstractmethod
    async def get_balance(self) -> list[Balance]:
        """Return all account balances across all currencies."""
        ...

    @abstractmethod
    async def get_positions(self) -> list[Position]:
        """Return all currently open positions."""
        ...

    # ── Orders ────────────────────────────────────────────────────────────────

    @abstractmethod
    async def place_order(self, order: Order) -> OrderResult:
        """
        Place an order. Must be idempotent — if client_order_id already
        exists, return the existing order rather than creating a duplicate.
        """
        ...

    @abstractmethod
    async def cancel_order(self, broker_order_id: str) -> bool:
        """Cancel an open order. Return True if successfully cancelled."""
        ...

    @abstractmethod
    async def get_order(self, broker_order_id: str) -> OrderResult:
        """Get current status of an order by broker order ID."""
        ...

    @abstractmethod
    async def get_open_orders(self) -> list[OrderResult]:
        """Return all currently open orders."""
        ...

    # ── Market Data ───────────────────────────────────────────────────────────

    @abstractmethod
    async def get_candles(
        self,
        symbol: str,
        timeframe: str,
        limit: int = 200
    ) -> list[Candle]:
        """Fetch historical OHLCV candles."""
        ...

    @abstractmethod
    async def get_order_book(self, symbol: str, depth: int = 10) -> OrderBook:
        """Fetch current order book snapshot."""
        ...

    @abstractmethod
    async def get_last_price(self, symbol: str) -> Decimal:
        """Fetch last traded price for a symbol."""
        ...

    @abstractmethod
    async def stream_prices(
        self,
        symbols: list[str]
    ) -> AsyncIterator[Tick]:
        """
        Stream real-time price ticks via WebSocket.
        Yields Tick objects as they arrive.
        """
        ...

    # ── Broker Info ───────────────────────────────────────────────────────────

    @abstractmethod
    async def get_supported_symbols(self) -> list[str]:
        """Return list of all tradeable symbols on this broker."""
        ...

    @abstractmethod
    async def get_asset_class(self, symbol: str) -> AssetClass:
        """Return the asset class for a given symbol."""
        ...

    # ── Utility ───────────────────────────────────────────────────────────────

    def __repr__(self) -> str:
        mode = "PAPER" if self.paper_mode else "LIVE"
        return f"<{self.__class__.__name__} broker={self.broker_name} mode={mode}>"
