"""
brokers/binance/adapter.py
===========================
Binance spot adapter (REST via python-binance Client, asyncio.to_thread).

SDK: python-binance
Docs: https://python-binance.readthedocs.io/
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, AsyncIterator, Callable, TypeVar

from binance.client import Client
from binance.exceptions import BinanceAPIException
from loguru import logger

from brokers.base import (
    AssetClass,
    Balance,
    BrokerAdapter,
    Candle,
    Order,
    OrderBook,
    OrderResult,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
    Tick,
)
from brokers.rest_rate_limit import AsyncRestGap

T = TypeVar("T")

_KLINE_INTERVAL: dict[str, str] = {
    "1m": Client.KLINE_INTERVAL_1MINUTE,
    "5m": Client.KLINE_INTERVAL_5MINUTE,
    "15m": Client.KLINE_INTERVAL_15MINUTE,
    "30m": Client.KLINE_INTERVAL_30MINUTE,
    "1h": Client.KLINE_INTERVAL_1HOUR,
    "4h": Client.KLINE_INTERVAL_4HOUR,
    "1d": Client.KLINE_INTERVAL_1DAY,
}


def _d(v: object) -> Decimal:
    if v is None:
        return Decimal(0)
    if isinstance(v, Decimal):
        return v
    if isinstance(v, int):
        return Decimal(int(v))
    if isinstance(v, float) and v != v:
        return Decimal(0)
    return Decimal(str(v))


def _iso_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _binance_symbol(symbol: str) -> str:
    """BTC/USDT -> BTCUSDT."""
    return symbol.strip().upper().replace(" ", "").replace("/", "")


def _ms_to_iso(ms: int | None) -> str:
    if not ms:
        return _iso_now()
    return (
        datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _map_order_status(s: str | None) -> OrderStatus:
    m = {
        "NEW": OrderStatus.OPEN,
        "PARTIALLY_FILLED": OrderStatus.PARTIALLY_FILLED,
        "FILLED": OrderStatus.FILLED,
        "CANCELED": OrderStatus.CANCELLED,
        "CANCELLED": OrderStatus.CANCELLED,
        "PENDING_CANCEL": OrderStatus.PENDING,
        "REJECTED": OrderStatus.REJECTED,
        "EXPIRED": OrderStatus.CANCELLED,
    }
    return m.get((s or "").upper(), OrderStatus.PENDING)


def _binance_order_to_result(d: dict[str, Any]) -> OrderResult:
    oid = str(d["orderId"])
    sym = str(d.get("symbol", ""))
    side = OrderSide.BUY if d.get("side") == "BUY" else OrderSide.SELL
    st = _map_order_status(d.get("status"))
    qty = _d(d.get("origQty", "0"))
    filled = _d(d.get("executedQty", "0"))
    avg: Decimal | None = None
    ap = d.get("avgPrice") or d.get("price")
    if ap and _d(ap) > 0:
        avg = _d(ap)
    elif filled > 0 and d.get("cummulativeQuoteQty"):
        cq = _d(d["cummulativeQuoteQty"])
        if cq > 0:
            avg = (cq / filled).quantize(Decimal("0.00000001"))
    fee: Decimal | None = None
    fills = d.get("fills")
    if isinstance(fills, list) and fills:
        fee = Decimal(0)
        for f in fills:
            fee += _d(f.get("commission", "0"))
        if fee == 0:
            fee = None
    ts = _ms_to_iso(d.get("updateTime") or d.get("transactTime") or d.get("time"))
    return OrderResult(
        broker_order_id=oid,
        client_order_id=d.get("clientOrderId") or d.get("origClientOrderId"),
        status=st,
        symbol=sym,
        side=side,
        quantity=qty,
        filled_quantity=filled,
        avg_fill_price=avg,
        fee=fee,
        timestamp=ts,
    )


class BinanceAdapter(BrokerAdapter):
    """
    Binance spot REST adapter. ``paper_mode=True`` skips sending orders to the
    exchange (placeholder REJECTED result). Use ``testnet=True`` with keys from
    https://testnet.binance.vision/ for sandbox trading.
    """

    broker_name = "binance"

    def __init__(
        self,
        api_key: str = "",
        api_secret: str = "",
        paper_mode: bool = True,
        testnet: bool = False,
        tld: str = "com",
        **kwargs: Any,
    ) -> None:
        rest_iv = kwargs.pop("rest_min_interval_sec", None)
        _ = kwargs
        self.api_key = api_key or ""
        self.api_secret = api_secret or ""
        self.paper_mode = paper_mode
        self.testnet = testnet
        self.tld = tld
        if rest_iv is not None:
            self._rest_gap = AsyncRestGap(float(rest_iv))
        else:
            self._rest_gap = AsyncRestGap.from_env("BINANCE", default_seconds=0.055)
        self._lock = asyncio.Lock()
        self._connected = False
        self._private_ok = False
        self._client: Client | None = None
        self._order_symbol: dict[str, str] = {}

    @staticmethod
    def _is_time_skew_error(exc: Exception) -> bool:
        if isinstance(exc, BinanceAPIException):
            return int(getattr(exc, "code", 0)) == -1021
        return "timestamp for this request was" in str(exc).lower()

    async def _sync_time_offset(self) -> None:
        if self._client is None:
            return
        server = await self._run_sync(lambda: self._client.get_server_time())  # type: ignore[union-attr]
        server_ms = int(server.get("serverTime", 0))
        if server_ms <= 0:
            return
        local_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        self._client.timestamp_offset = server_ms - local_ms

    async def _call_private_with_time_sync(self, fn: Callable[[], T]) -> T:
        try:
            return await self._run_sync(fn)
        except Exception as exc:  # noqa: BLE001
            if not self._is_time_skew_error(exc):
                raise
            logger.warning("binance | timestamp skew detected (-1021) — syncing server time and retrying")
            await self._sync_time_offset()
            return await self._run_sync(fn)

    async def _run_sync(self, fn: Callable[[], T]) -> T:
        async with self._lock:
            await self._rest_gap.wait()
            return await asyncio.to_thread(fn)

    def _require_private(self) -> None:
        if not self._private_ok or self._client is None:
            raise RuntimeError("Binance private API not available (connect with API keys)")

    def _remember_order(self, order_id: str, symbol: str) -> None:
        self._order_symbol[order_id] = _binance_symbol(symbol)

    async def _resolve_symbol_for_order_id(self, order_id: str) -> str | None:
        if order_id in self._order_symbol:
            return self._order_symbol[order_id]
        if self._client is None:
            return None

        def _scan() -> str | None:
            opens = self._client.get_open_orders()
            for o in opens:
                if str(o.get("orderId")) == order_id:
                    return str(o["symbol"])
            return None

        sym = await self._run_sync(_scan)
        if sym:
            self._order_symbol[order_id] = sym
        return sym

    async def connect(self) -> bool:
        try:
            self._client = Client(
                api_key=self.api_key or None,
                api_secret=self.api_secret or None,
                testnet=self.testnet,
                tld=self.tld,
            )
            await self._sync_time_offset()
            if self.api_key and self.api_secret:
                await self._call_private_with_time_sync(lambda: self._client.get_account())  # type: ignore[union-attr]
                self._private_ok = True
                logger.info(
                    "connect | Binance | private API | ok | testnet={}",
                    self.testnet,
                )
            else:
                self._private_ok = False
                if not self.paper_mode:
                    logger.error(
                        "connect | Binance | live mode requires BINANCE_API_KEY and BINANCE_API_SECRET"
                    )
                    self._connected = False
                    return False
                logger.info("connect | Binance | public only (no API keys)")

            self._connected = True
            logger.info(
                "connect | Binance | paper_mode={} | private={} | testnet={}",
                self.paper_mode,
                self._private_ok,
                self.testnet,
            )
            return True
        except Exception as exc:  # noqa: BLE001
            logger.exception("connect | Binance | failed | error={}", exc)
            self._connected = False
            self._private_ok = False
            self._client = None
            return False

    async def disconnect(self) -> None:
        self._connected = False
        self._private_ok = False
        self._client = None
        self._order_symbol.clear()
        logger.info("disconnect | Binance | done")

    async def is_connected(self) -> bool:
        return self._connected

    async def get_balance(self) -> list[Balance]:
        # Synthetic paper wallet (no Binance-native paper account): in
        # paper mode report ledger-derived venue equity so crypto P&L
        # flows into NAV. CRYPTO_PAPER_WALLET=0 disables.
        if self.paper_mode:
            from system.paper_wallet import venue_equity

            eq = venue_equity("binance")
            if eq is not None:
                return [
                    Balance(
                        currency="USD",
                        total=eq,
                        available=eq,
                        reserved=Decimal("0"),
                    )
                ]
        if not self._private_ok or self._client is None:
            return []

        def _fetch() -> dict[str, Any]:
            assert self._client is not None
            return self._client.get_account()

        raw = await self._call_private_with_time_sync(_fetch)
        out: list[Balance] = []
        for row in raw.get("balances", []):
            free = _d(row.get("free", "0"))
            locked = _d(row.get("locked", "0"))
            total = free + locked
            if total == 0:
                continue
            out.append(
                Balance(
                    currency=str(row.get("asset", "")),
                    total=total,
                    available=free,
                    reserved=locked,
                )
            )
        if not out:
            n = len(raw.get("balances", []))
            if n:
                logger.info(
                    "get_balance | Binance | {} balance row(s), all zero",
                    n,
                )
            else:
                logger.info("get_balance | Binance | no balance rows in account response")
        return out

    async def get_positions(self) -> list[Position]:
        return []

    async def place_order(self, order: Order) -> OrderResult:
        if order.order_type in (OrderType.STOP, OrderType.STOP_LIMIT):
            raise ValueError("Binance spot adapter: STOP / STOP_LIMIT not implemented")

        sym = _binance_symbol(order.symbol)
        side = "BUY" if order.side == OrderSide.BUY else "SELL"
        tif = (order.time_in_force or "GTC").upper()

        if self.paper_mode:
            logger.warning(
                "place_order | Binance | paper_mode | not sending | symbol={} side={} qty={}",
                sym,
                side,
                order.quantity,
            )
            meta = dict(order.instrument_metadata or {})
            meta.setdefault("error_message", "Binance has no native paper order placement; order was not sent")
            meta.setdefault("reject_reason", "paper_mode_no_native_order")
            meta.setdefault("rejected_by", "binance")
            order.instrument_metadata = meta
            return OrderResult(
                broker_order_id="",
                client_order_id=order.client_order_id,
                status=OrderStatus.REJECTED,
                symbol=sym,
                side=order.side,
                quantity=order.quantity,
                filled_quantity=Decimal(0),
                avg_fill_price=None,
                fee=None,
                timestamp=_iso_now(),
            )

        self._require_private()
        assert self._client is not None

        if order.client_order_id:

            def _dup_check() -> dict[str, Any] | None:
                try:
                    return self._client.get_order(
                        symbol=sym,
                        origClientOrderId=order.client_order_id,
                    )
                except BinanceAPIException:
                    return None

            existing = await self._run_sync(_dup_check)
            if existing and existing.get("orderId") is not None:
                oid = str(existing["orderId"])
                self._remember_order(oid, sym)
                logger.info("place_order | Binance | idempotent | orderId={}", oid)
                return _binance_order_to_result(existing)

        def _submit() -> dict[str, Any]:
            assert self._client is not None
            params: dict[str, Any] = {
                "symbol": sym,
                "side": side,
                "quantity": str(order.quantity),
            }
            if order.client_order_id:
                params["newClientOrderId"] = order.client_order_id
            if order.order_type == OrderType.MARKET:
                params["type"] = Client.ORDER_TYPE_MARKET
            elif order.order_type == OrderType.LIMIT:
                if order.limit_price is None:
                    raise ValueError("limit_price required for LIMIT")
                params["type"] = Client.ORDER_TYPE_LIMIT
                params["timeInForce"] = tif
                params["price"] = str(order.limit_price)
            else:
                raise ValueError(f"Unsupported order type: {order.order_type}")
            return self._client.create_order(**params)

        try:
            resp = await self._run_sync(_submit)
            oid = str(resp["orderId"])
            self._remember_order(oid, sym)
            return _binance_order_to_result(resp)
        except Exception as exc:  # noqa: BLE001
            logger.exception("place_order | Binance | error={}", exc)
            meta = dict(order.instrument_metadata or {})
            meta.setdefault("error_message", str(exc)[:500])
            meta.setdefault("reject_reason", str(exc)[:200])
            meta.setdefault("rejected_by", "binance")
            order.instrument_metadata = meta
            return OrderResult(
                broker_order_id="",
                client_order_id=order.client_order_id,
                status=OrderStatus.REJECTED,
                symbol=sym,
                side=order.side,
                quantity=order.quantity,
                filled_quantity=Decimal(0),
                avg_fill_price=None,
                fee=None,
                timestamp=_iso_now(),
            )

    async def cancel_order(self, broker_order_id: str) -> bool:
        self._require_private()
        sym = await self._resolve_symbol_for_order_id(broker_order_id)
        if sym is None:
            logger.warning("cancel_order | Binance | unknown symbol for orderId={}", broker_order_id)
            return False

        def _go() -> dict[str, Any]:
            assert self._client is not None
            return self._client.cancel_order(symbol=sym, orderId=int(broker_order_id))

        try:
            await self._run_sync(_go)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("cancel_order | Binance | orderId={} | error={}", broker_order_id, exc)
            return False

    async def get_order(self, broker_order_id: str) -> OrderResult:
        self._require_private()
        sym = await self._resolve_symbol_for_order_id(broker_order_id)
        if sym is None:
            return OrderResult(
                broker_order_id=broker_order_id,
                client_order_id=None,
                status=OrderStatus.REJECTED,
                symbol="",
                side=OrderSide.BUY,
                quantity=Decimal(0),
                filled_quantity=Decimal(0),
                avg_fill_price=None,
                fee=None,
                timestamp=_iso_now(),
            )

        def _go() -> dict[str, Any]:
            assert self._client is not None
            return self._client.get_order(symbol=sym, orderId=int(broker_order_id))

        try:
            raw = await self._run_sync(_go)
            oid = str(raw.get("orderId", broker_order_id))
            self._remember_order(oid, sym)
            return _binance_order_to_result(raw)
        except Exception as exc:  # noqa: BLE001
            logger.warning("get_order | Binance | orderId={} | error={}", broker_order_id, exc)
            return OrderResult(
                broker_order_id=broker_order_id,
                client_order_id=None,
                status=OrderStatus.REJECTED,
                symbol=sym,
                side=OrderSide.BUY,
                quantity=Decimal(0),
                filled_quantity=Decimal(0),
                avg_fill_price=None,
                fee=None,
                timestamp=_iso_now(),
            )

    async def get_open_orders(self) -> list[OrderResult]:
        if not self._private_ok or self._client is None:
            return []

        def _go() -> list[dict[str, Any]]:
            assert self._client is not None
            return self._client.get_open_orders()

        raw = await self._run_sync(_go)
        out: list[OrderResult] = []
        for o in raw:
            r = _binance_order_to_result(o)
            self._remember_order(r.broker_order_id, r.symbol)
            out.append(r)
        return out

    async def get_candles(self, symbol: str, timeframe: str, limit: int = 200) -> list[Candle]:
        if self._client is None:
            return []
        interval = _KLINE_INTERVAL.get(timeframe)
        if interval is None:
            raise ValueError(f"Unsupported timeframe: {timeframe}")
        sym = _binance_symbol(symbol)

        def _go() -> list[list[Any]]:
            assert self._client is not None
            return self._client.get_klines(symbol=sym, interval=interval, limit=limit)

        rows = await self._run_sync(_go)
        out: list[Candle] = []
        for k in rows:
            ts_ms, o, h, lo, c, vol = int(k[0]), k[1], k[2], k[3], k[4], k[5]
            ts = datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc).replace(microsecond=0)
            out.append(
                Candle(
                    symbol=symbol,
                    timestamp=ts.isoformat().replace("+00:00", "Z"),
                    open=_d(o),
                    high=_d(h),
                    low=_d(lo),
                    close=_d(c),
                    volume=_d(vol),
                    timeframe=timeframe,
                )
            )
        return out

    async def get_order_book(self, symbol: str, depth: int = 10) -> OrderBook:
        if self._client is None:
            raise RuntimeError("not connected")
        sym = _binance_symbol(symbol)
        allowed = (5, 10, 20, 50, 100, 500, 1000, 5000)
        d = max(1, min(depth, 5000))
        lim = next((x for x in allowed if x >= d), 5000)

        def _go() -> dict[str, Any]:
            assert self._client is not None
            return self._client.get_order_book(symbol=sym, limit=lim)

        raw = await self._run_sync(_go)
        bids: list[tuple[Decimal, Decimal]] = []
        asks: list[tuple[Decimal, Decimal]] = []
        for price, qty, *_ in raw.get("bids", [])[:depth]:
            bids.append((_d(price), _d(qty)))
        for price, qty, *_ in raw.get("asks", [])[:depth]:
            asks.append((_d(price), _d(qty)))
        return OrderBook(symbol=symbol, timestamp=_iso_now(), bids=bids, asks=asks)

    async def get_last_price(self, symbol: str) -> Decimal:
        if self._client is None:
            return Decimal(0)
        sym = _binance_symbol(symbol)

        def _go() -> dict[str, Any]:
            assert self._client is not None
            return self._client.get_symbol_ticker(symbol=sym)

        raw = await self._run_sync(_go)
        return _d(raw.get("price", "0"))

    async def stream_prices(self, symbols: list[str]) -> AsyncIterator[Tick]:
        if not symbols:
            return
        while self._connected:
            for sym in symbols:
                if not self._connected:
                    break
                try:
                    px = await self.get_last_price(sym)
                    ob = await self.get_order_book(sym, depth=5)
                    bid = ob.bids[0][0] if ob.bids else None
                    ask = ob.asks[0][0] if ob.asks else None
                    yield Tick(
                        symbol=sym,
                        timestamp=_iso_now(),
                        price=px,
                        volume=Decimal(0),
                        bid=bid,
                        ask=ask,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("stream_prices | Binance | symbol={} | error={}", sym, exc)
            await asyncio.sleep(1.0)

    async def get_supported_symbols(self) -> list[str]:
        if self._client is None:
            return []

        def _go() -> dict[str, Any]:
            assert self._client is not None
            return self._client.get_exchange_info()

        raw = await self._run_sync(_go)
        out: list[str] = []
        for s in raw.get("symbols", []):
            if s.get("status") == "TRADING":
                out.append(str(s.get("symbol", "")))
        return sorted({x for x in out if x})

    async def get_asset_class(self, symbol: str) -> AssetClass:
        return AssetClass.CRYPTO
