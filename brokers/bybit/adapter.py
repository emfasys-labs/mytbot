"""
brokers/bybit/adapter.py
========================
Bybit V5 unified trading (spot or USDT linear perps).

SDK: pybit
Docs: https://bybit-exchange.github.io/docs/v5/intro
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable, TypeVar

from loguru import logger
from pybit.exceptions import InvalidRequestError
from pybit.unified_trading import HTTP

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

T = TypeVar("T")

_BYBIT_INTERVAL: dict[str, str] = {
    "1m": "1",
    "3m": "3",
    "5m": "5",
    "15m": "15",
    "30m": "30",
    "1h": "60",
    "2h": "120",
    "4h": "240",
    "1d": "D",
}


def _d(v: object) -> Decimal:
    if v is None:
        return Decimal(0)
    if isinstance(v, Decimal):
        return v
    if isinstance(v, (int, float)):
        if isinstance(v, float) and v != v:
            return Decimal(0)
        return Decimal(str(v))
    return Decimal(str(v))


def _iso_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _bybit_symbol(symbol: str) -> str:
    """BTC/USDT -> BTCUSDT."""
    return symbol.strip().upper().replace(" ", "").replace("/", "").replace("-", "")


def _map_order_status(s: str | None) -> OrderStatus:
    m = {
        "Created": OrderStatus.OPEN,
        "New": OrderStatus.OPEN,
        "PartiallyFilled": OrderStatus.PARTIALLY_FILLED,
        "Filled": OrderStatus.FILLED,
        "Cancelled": OrderStatus.CANCELLED,
        "Rejected": OrderStatus.REJECTED,
        "Deactivated": OrderStatus.CANCELLED,
    }
    return m.get((s or "").strip(), OrderStatus.PENDING)


def _order_result_from_bybit(
    d: dict[str, Any],
    *,
    fallback_symbol: str,
    fallback_side: OrderSide,
) -> OrderResult:
    oid = str(d.get("orderId", "") or d.get("orderLinkId", "") or "")
    sym = str(d.get("symbol", fallback_symbol))
    side_raw = str(d.get("side", "")).lower()
    side = OrderSide.BUY if side_raw == "buy" else OrderSide.SELL
    st = _map_order_status(d.get("orderStatus"))
    qty = _d(d.get("qty", d.get("origQty", "0")))
    filled = _d(d.get("cumExecQty", "0"))
    avg: Decimal | None = None
    ap = d.get("avgPrice")
    if ap and _d(ap) > 0:
        avg = _d(ap)
    ts = _iso_now()
    ut = d.get("updatedTime") or d.get("createdTime")
    if ut:
        try:
            ts = (
                datetime.fromtimestamp(int(ut) / 1000.0, tz=timezone.utc)
                .replace(microsecond=0)
                .isoformat()
                .replace("+00:00", "Z")
            )
        except (TypeError, ValueError, OSError):
            pass
    return OrderResult(
        broker_order_id=oid,
        client_order_id=d.get("orderLinkId"),
        status=st,
        symbol=sym,
        side=side,
        quantity=qty,
        filled_quantity=filled,
        avg_fill_price=avg,
        fee=None,
        timestamp=ts,
    )


class BybitAdapter(BrokerAdapter):
    """
    Bybit V5 adapter. ``category`` is ``spot`` or ``linear`` (USDT perpetuals).

    ``paper_mode=True`` does not submit live orders (returns REJECTED placeholder).
    Use ``testnet=True`` with keys from Bybit testnet for sandbox trading.
    """

    broker_name = "bybit"

    def __init__(
        self,
        api_key: str = "",
        api_secret: str = "",
        paper_mode: bool = True,
        testnet: bool = False,
        category: str = "linear",
        **kwargs: Any,
    ) -> None:
        _ = kwargs
        self.api_key = (api_key or "").strip()
        self.api_secret = (api_secret or "").strip()
        self.paper_mode = paper_mode
        self.testnet = testnet
        self.category = (category or "linear").strip().lower()
        if self.category not in ("spot", "linear"):
            raise ValueError("Bybit category must be 'spot' or 'linear'")
        self._lock = asyncio.Lock()
        self._connected = False
        self._private_ok = False
        self._client: HTTP | None = None
        self._order_symbol: dict[str, str] = {}

    async def _run_sync(self, fn: Callable[[], T]) -> T:
        async with self._lock:
            return await asyncio.to_thread(fn)

    def _require_private(self) -> None:
        if not self._private_ok or self._client is None:
            raise RuntimeError("Bybit private API not available (connect with API keys)")

    async def connect(self) -> bool:
        try:
            self._client = HTTP(
                testnet=self.testnet,
                api_key=self.api_key or None,
                api_secret=self.api_secret or None,
            )
            await self._run_sync(lambda: self._client.get_server_time())  # type: ignore[union-attr]
            if self.api_key and self.api_secret:

                def _ping_private() -> None:
                    assert self._client is not None
                    self._client.get_wallet_balance(accountType="UNIFIED")

                await self._run_sync(_ping_private)
                self._private_ok = True
                logger.info(
                    "connect | Bybit | private API | ok | testnet={} category={}",
                    self.testnet,
                    self.category,
                )
            else:
                self._private_ok = False
                if not self.paper_mode:
                    logger.error(
                        "connect | Bybit | live mode requires BYBIT_API_KEY and BYBIT_API_SECRET"
                    )
                    self._connected = False
                    return False
                logger.info("connect | Bybit | public only (no API keys)")

            self._connected = True
            logger.info(
                "connect | Bybit | paper_mode={} | private={} | testnet={} | category={}",
                self.paper_mode,
                self._private_ok,
                self.testnet,
                self.category,
            )
            return True
        except Exception as exc:  # noqa: BLE001
            logger.exception("connect | Bybit | failed | error={}", exc)
            self._connected = False
            self._private_ok = False
            self._client = None
            return False

    async def disconnect(self) -> None:
        self._connected = False
        self._private_ok = False
        self._client = None
        self._order_symbol.clear()
        logger.info("disconnect | Bybit | done")

    async def is_connected(self) -> bool:
        return self._connected

    async def get_balance(self) -> list[Balance]:
        if not self._private_ok or self._client is None:
            return []

        def _fetch() -> dict[str, Any]:
            assert self._client is not None
            return self._client.get_wallet_balance(accountType="UNIFIED")

        try:
            raw = await self._run_sync(_fetch)
        except Exception as exc:  # noqa: BLE001
            logger.warning("get_balance | Bybit | error={}", exc)
            return []

        out: list[Balance] = []
        lst = (raw.get("result", {}) or {}).get("list") or []
        for acct in lst:
            for c in acct.get("coin", []) or []:
                cur = str(c.get("coin", ""))
                if not cur:
                    continue
                wallet = _d(c.get("walletBalance", "0"))
                locked = _d(c.get("locked", "0"))
                avail = _d(c.get("availableToWithdraw", c.get("availableBalance", "0")))
                if wallet <= 0 and avail <= 0:
                    continue
                out.append(
                    Balance(
                        currency=cur,
                        total=wallet,
                        available=avail,
                        reserved=locked,
                    )
                )
        return out

    async def get_positions(self) -> list[Position]:
        if self.category != "linear" or not self._private_ok or self._client is None:
            return []

        def _fetch() -> dict[str, Any]:
            assert self._client is not None
            return self._client.get_positions(category="linear", settleCoin="USDT")

        try:
            raw = await self._run_sync(_fetch)
        except Exception as exc:  # noqa: BLE001
            logger.warning("get_positions | Bybit | error={}", exc)
            return []

        out: list[Position] = []
        for row in (raw.get("result", {}) or {}).get("list") or []:
            sz = _d(row.get("size", "0"))
            if sz == 0:
                continue
            sym = str(row.get("symbol", ""))
            side = str(row.get("side", "")).lower()
            qty = sz if side == "buy" else -sz
            ep = _d(row.get("avgPrice", row.get("entryPrice", "0")))
            mp = _d(row.get("markPrice", ep))
            upnl = _d(row.get("unrealisedPnl", "0"))
            out.append(
                Position(
                    symbol=sym,
                    asset_class=AssetClass.FUTURE,
                    quantity=qty,
                    avg_entry_price=ep,
                    current_price=mp,
                    unrealised_pnl=upnl,
                    broker=self.broker_name,
                )
            )
        return out

    async def place_order(self, order: Order) -> OrderResult:
        if order.order_type in (OrderType.STOP, OrderType.STOP_LIMIT):
            raise ValueError("Bybit adapter: STOP / STOP_LIMIT not implemented in this path")

        sym = _bybit_symbol(order.symbol)
        side = "Buy" if order.side == OrderSide.BUY else "Sell"

        if self.paper_mode:
            logger.warning(
                "place_order | Bybit | paper_mode | not sending | symbol={} side={} qty={}",
                sym,
                side,
                order.quantity,
            )
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

        ot = "Market" if order.order_type == OrderType.MARKET else "Limit"
        params: dict[str, Any] = {
            "category": self.category,
            "symbol": sym,
            "side": side,
            "orderType": ot,
            "qty": str(order.quantity),
        }
        if order.client_order_id:
            params["orderLinkId"] = str(order.client_order_id)[:45]
        if ot == "Limit":
            if order.limit_price is None:
                raise ValueError("limit_price required for LIMIT")
            params["price"] = str(order.limit_price)
            params["timeInForce"] = (order.time_in_force or "GTC").upper()

        def _submit() -> dict[str, Any]:
            assert self._client is not None
            return self._client.place_order(**params)

        try:
            raw = await self._run_sync(_submit)
            res = (raw.get("result") or {}) if isinstance(raw, dict) else {}
            oid = str(res.get("orderId", "") or "")
            if oid:
                self._order_symbol[oid] = sym
            d = {
                "orderId": res.get("orderId"),
                "orderLinkId": res.get("orderLinkId") or order.client_order_id,
                "symbol": sym,
                "side": side.lower(),
                "orderStatus": "New",
                "qty": str(order.quantity),
                "cumExecQty": "0",
            }
            return _order_result_from_bybit(d, fallback_symbol=sym, fallback_side=order.side)
        except InvalidRequestError as exc:
            msg = getattr(exc, "message", str(exc))
            if order.client_order_id and "duplicate" in msg.lower():
                return await self.get_order_by_link(sym, order.client_order_id)
            logger.warning("place_order | Bybit | error={}", exc)
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
        except Exception as exc:  # noqa: BLE001
            logger.exception("place_order | Bybit | error={}", exc)
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

    async def get_order_by_link(self, symbol: str, order_link_id: str) -> OrderResult:
        self._require_private()
        assert self._client is not None
        sym = _bybit_symbol(symbol)

        def _go() -> dict[str, Any]:
            assert self._client is not None
            return self._client.get_open_orders(
                category=self.category,
                symbol=sym,
                orderLinkId=order_link_id,
            )

        try:
            raw = await self._run_sync(_go)
            lst = (raw.get("result", {}) or {}).get("list") or []
            if lst:
                return _order_result_from_bybit(
                    lst[0], fallback_symbol=sym, fallback_side=OrderSide.BUY
                )
        except Exception:  # noqa: BLE001
            pass

        def _hist() -> dict[str, Any]:
            assert self._client is not None
            return self._client.get_order_history(
                category=self.category,
                symbol=sym,
                orderLinkId=order_link_id,
                limit=1,
            )

        try:
            raw = await self._run_sync(_hist)
            lst = (raw.get("result", {}) or {}).get("list") or []
            if lst:
                return _order_result_from_bybit(
                    lst[0], fallback_symbol=sym, fallback_side=OrderSide.BUY
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("get_order_by_link | Bybit | error={}", exc)

        return OrderResult(
            broker_order_id="",
            client_order_id=order_link_id,
            status=OrderStatus.REJECTED,
            symbol=sym,
            side=OrderSide.BUY,
            quantity=Decimal(0),
            filled_quantity=Decimal(0),
            avg_fill_price=None,
            fee=None,
            timestamp=_iso_now(),
        )

    async def cancel_order(self, broker_order_id: str) -> bool:
        self._require_private()
        assert self._client is not None
        sym = self._order_symbol.get(broker_order_id)
        if not sym:
            for o in await self.get_open_orders():
                if o.broker_order_id == broker_order_id:
                    sym = o.symbol
                    break
        if not sym:
            logger.warning("cancel_order | Bybit | unknown symbol for orderId={}", broker_order_id)
            return False

        def _go() -> dict[str, Any]:
            assert self._client is not None
            return self._client.cancel_order(
                category=self.category,
                symbol=sym,
                orderId=broker_order_id,
            )

        try:
            await self._run_sync(_go)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("cancel_order | Bybit | orderId={} | error={}", broker_order_id, exc)
            return False

    async def get_order(self, broker_order_id: str) -> OrderResult:
        self._require_private()
        assert self._client is not None

        def _go() -> dict[str, Any]:
            assert self._client is not None
            return self._client.get_open_orders(category=self.category, orderId=broker_order_id)

        try:
            raw = await self._run_sync(_go)
            lst = (raw.get("result", {}) or {}).get("list") or []
            if lst:
                return _order_result_from_bybit(
                    lst[0], fallback_symbol=str(lst[0].get("symbol", "")), fallback_side=OrderSide.BUY
                )
        except Exception:  # noqa: BLE001
            pass

        def _hist() -> dict[str, Any]:
            assert self._client is not None
            return self._client.get_order_history(
                category=self.category,
                orderId=broker_order_id,
                limit=1,
            )

        try:
            raw = await self._run_sync(_hist)
            lst = (raw.get("result", {}) or {}).get("list") or []
            if lst:
                return _order_result_from_bybit(
                    lst[0], fallback_symbol=str(lst[0].get("symbol", "")), fallback_side=OrderSide.BUY
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("get_order | Bybit | orderId={} | error={}", broker_order_id, exc)

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

    async def get_open_orders(self) -> list[OrderResult]:
        if not self._private_ok or self._client is None:
            return []

        def _go() -> dict[str, Any]:
            assert self._client is not None
            return self._client.get_open_orders(category=self.category, limit=50)

        try:
            raw = await self._run_sync(_go)
        except Exception as exc:  # noqa: BLE001
            logger.warning("get_open_orders | Bybit | error={}", exc)
            return []

        out: list[OrderResult] = []
        for row in (raw.get("result", {}) or {}).get("list") or []:
            out.append(
                _order_result_from_bybit(
                    row,
                    fallback_symbol=str(row.get("symbol", "")),
                    fallback_side=OrderSide.BUY,
                )
            )
        return out

    async def get_candles(self, symbol: str, timeframe: str, limit: int = 200) -> list[Candle]:
        if self._client is None:
            return []
        interval = _BYBIT_INTERVAL.get(timeframe)
        if interval is None:
            logger.warning("get_candles | Bybit | unsupported timeframe={}", timeframe)
            return []
        sym = _bybit_symbol(symbol)

        def _go() -> dict[str, Any]:
            assert self._client is not None
            return self._client.get_kline(
                category=self.category,
                symbol=sym,
                interval=interval,
                limit=min(limit, 1000),
            )

        try:
            raw = await self._run_sync(_go)
        except Exception as exc:  # noqa: BLE001
            logger.warning("get_candles | Bybit | error={}", exc)
            return []

        out: list[Candle] = []
        for row in (raw.get("result", {}) or {}).get("list") or []:
            ts_ms, o, h, l, c, vol, _turnover = row[0], row[1], row[2], row[3], row[4], row[5], row[6]
            ts = (
                datetime.fromtimestamp(int(ts_ms) / 1000.0, tz=timezone.utc)
                .replace(microsecond=0)
                .isoformat()
                .replace("+00:00", "Z")
            )
            out.append(
                Candle(
                    symbol=sym,
                    timestamp=ts,
                    open=_d(o),
                    high=_d(h),
                    low=_d(l),
                    close=_d(c),
                    volume=_d(vol),
                    timeframe=timeframe,
                )
            )
        out.reverse()
        return out

    async def get_order_book(self, symbol: str, depth: int = 10) -> OrderBook:
        if self._client is None:
            return OrderBook(symbol=_bybit_symbol(symbol), timestamp=_iso_now(), bids=[], asks=[])
        sym = _bybit_symbol(symbol)

        def _go() -> dict[str, Any]:
            assert self._client is not None
            return self._client.get_orderbook(category=self.category, symbol=sym, limit=depth)

        try:
            raw = await self._run_sync(_go)
        except Exception as exc:  # noqa: BLE001
            logger.warning("get_order_book | Bybit | error={}", exc)
            return OrderBook(symbol=sym, timestamp=_iso_now(), bids=[], asks=[])

        res = (raw.get("result", {}) or {})
        bids = [(_d(p), _d(s)) for p, s in (res.get("b") or [])[:depth]]
        asks = [(_d(p), _d(s)) for p, s in (res.get("a") or [])[:depth]]
        ts = _iso_now()
        ut = res.get("ts")
        if ut:
            try:
                ts = (
                    datetime.fromtimestamp(int(ut) / 1000.0, tz=timezone.utc)
                    .replace(microsecond=0)
                    .isoformat()
                    .replace("+00:00", "Z")
                )
            except (TypeError, ValueError, OSError):
                pass
        return OrderBook(symbol=sym, timestamp=ts, bids=bids, asks=asks)

    async def get_last_price(self, symbol: str) -> Decimal:
        if self._client is None:
            return Decimal(0)
        sym = _bybit_symbol(symbol)

        def _go() -> dict[str, Any]:
            assert self._client is not None
            return self._client.get_tickers(category=self.category, symbol=sym)

        try:
            raw = await self._run_sync(_go)
            lst = (raw.get("result", {}) or {}).get("list") or []
            if lst:
                lp = lst[0].get("lastPrice")
                if lp is not None:
                    return _d(lp)
        except Exception as exc:  # noqa: BLE001
            logger.warning("get_last_price | Bybit | error={}", exc)
        return Decimal(0)

    async def stream_prices(self, symbols: list[str]) -> AsyncIterator[Tick]:
        if self._client is None:
            return
        norm = [_bybit_symbol(s) for s in symbols if s.strip()]
        if not norm:
            return

        while True:
            for sym in norm:
                try:

                    def _t() -> dict[str, Any]:
                        assert self._client is not None
                        return self._client.get_tickers(category=self.category, symbol=sym)

                    raw = await self._run_sync(_t)
                    lst = (raw.get("result", {}) or {}).get("list") or []
                    if not lst:
                        continue
                    row = lst[0]
                    price = _d(row.get("lastPrice", "0"))
                    bid = _d(row.get("bid1Price", "0")) or None
                    ask = _d(row.get("ask1Price", "0")) or None
                    vol = _d(row.get("volume24h", "0"))
                    if price <= 0:
                        continue
                    yield Tick(
                        symbol=sym,
                        timestamp=_iso_now(),
                        price=price,
                        volume=vol,
                        bid=bid if bid and bid > 0 else None,
                        ask=ask if ask and ask > 0 else None,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    logger.debug("stream_prices | Bybit | {} | {}", sym, exc)
            await asyncio.sleep(2.0)

    async def get_supported_symbols(self) -> list[str]:
        if self._client is None:
            return []

        def _go() -> dict[str, Any]:
            assert self._client is not None
            return self._client.get_instruments_info(category=self.category, limit=500)

        try:
            raw = await self._run_sync(_go)
            lst = (raw.get("result", {}) or {}).get("list") or []
            return [str(x.get("symbol", "")) for x in lst if x.get("symbol")]
        except Exception as exc:  # noqa: BLE001
            logger.warning("get_supported_symbols | Bybit | error={}", exc)
            return []

    async def get_asset_class(self, symbol: str) -> AssetClass:
        if self.category == "linear":
            return AssetClass.FUTURE
        return AssetClass.CRYPTO

    async def fetch_funding_market_snapshot(self, symbol: str) -> dict[str, Any] | None:
        """
        Optional capability (not on ``BrokerAdapter`` ABC): linear perp funding + mark + top of book.
        Used by ``data.funding_rates.FundingRateDataProvider`` for funding-rate arbitrage scanning.
        """
        if self.category != "linear" or self._client is None:
            return None
        sym = _bybit_symbol(symbol)

        def _go() -> dict[str, Any]:
            assert self._client is not None
            return self._client.get_tickers(category="linear", symbol=sym)

        try:
            raw = await self._run_sync(_go)
        except Exception as exc:  # noqa: BLE001
            logger.warning("fetch_funding_market_snapshot | Bybit | error={}", exc)
            return None

        lst = (raw.get("result", {}) or {}).get("list") or []
        if not lst:
            return None
        row = lst[0]
        fr = row.get("fundingRate")
        nft = row.get("nextFundingTime")
        mark = row.get("markPrice") or row.get("lastPrice")
        bid = row.get("bid1Price")
        ask = row.get("ask1Price")
        next_dt: datetime | None = None
        if nft is not None:
            try:
                next_dt = datetime.fromtimestamp(int(nft) / 1000.0, tz=timezone.utc)
            except (TypeError, ValueError, OSError):
                next_dt = None

        return {
            "symbol": sym,
            "funding_rate": _d(fr) if fr is not None else Decimal("0"),
            "funding_interval_hours": 8,
            "next_funding_time": next_dt,
            "mark_price": _d(mark) if mark is not None else Decimal("0"),
            "bid": _d(bid) if bid is not None else Decimal("0"),
            "ask": _d(ask) if ask is not None else Decimal("0"),
        }

