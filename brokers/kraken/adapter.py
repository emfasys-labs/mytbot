"""
brokers/kraken/adapter.py
==========================
Kraken adapter — crypto spot (REST + simple ticker polling stream).

SDK: python-kraken-sdk (Market, User, Trade sync clients; wrapped with asyncio.to_thread).
Docs: https://docs.kraken.com/rest/
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, AsyncIterator, Callable, TypeVar

from loguru import logger


def _kraken_sdk_classes() -> tuple[Any, Any, Any]:
    """Import python-kraken-sdk on first use so ``main.py`` can start without it (e.g. IBKR-only)."""
    try:
        from kraken.spot import Market, Trade, User
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "Missing dependency for Kraken: install the project venv packages, e.g. "
            "`.venv\\Scripts\\pip install -r requirements.txt` (package `python-kraken-sdk`)."
        ) from exc
    return Market, Trade, User

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

_OHLC_INTERVAL_MINUTES: dict[str, int] = {
    "1m": 1,
    "5m": 5,
    "15m": 15,
    "30m": 30,
    "1h": 60,
    "4h": 240,
    "1d": 1440,
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


def _pair_altname(symbol: str) -> str:
    """Map human symbol to Kraken REST pair code (altname). Delegates to shared mapper."""
    from data.symbol_mapper import kraken_pair_altname

    return kraken_pair_altname(symbol)


def _userref_from_client_id(client_order_id: str | None) -> int | None:
    if not client_order_id:
        return None
    h = int(hashlib.sha256(client_order_id.encode()).hexdigest()[:8], 16)
    return h & 0x7FFFFFFF


def _unwrap_first_pair_value(data: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    if not data:
        raise ValueError("empty Kraken pair response")
    key = next(iter(data))
    return key, data[key]


def _kraken_status_to_order_status(status: str, vol: Decimal, vol_exec: Decimal) -> OrderStatus:
    st = (status or "").lower()
    if st == "open":
        if vol_exec > 0 and vol_exec < vol:
            return OrderStatus.PARTIALLY_FILLED
        return OrderStatus.OPEN
    if st == "closed":
        if vol > 0 and vol_exec >= vol:
            return OrderStatus.FILLED
        if vol_exec > 0:
            return OrderStatus.PARTIALLY_FILLED
        return OrderStatus.CANCELLED
    if st in ("canceled", "cancelled"):
        return OrderStatus.CANCELLED
    if st == "expired":
        return OrderStatus.CANCELLED
    return OrderStatus.PENDING


def _order_dict_to_result(txid: str, o: dict[str, Any], symbol_hint: str) -> OrderResult:
    descr = o.get("descr") or {}
    pair = str(descr.get("pair") or symbol_hint)
    side_s = str(descr.get("type") or "buy").lower()
    side = OrderSide.BUY if side_s == "buy" else OrderSide.SELL
    vol = _d(o.get("vol"))
    vol_exec = _d(o.get("vol_exec"))
    st = _kraken_status_to_order_status(str(o.get("status") or ""), vol, vol_exec)
    avg = o.get("price")
    avg_d = _d(avg) if avg not in (None, "0", "0.00000", "0.00000000") else None
    if avg_d is not None and avg_d == 0:
        avg_d = None
    fee = _d(o.get("fee")) if o.get("fee") is not None else None
    if fee == 0:
        fee = None
    return OrderResult(
        broker_order_id=txid,
        client_order_id=None,
        status=st,
        symbol=pair,
        side=side,
        quantity=vol,
        filled_quantity=vol_exec,
        avg_fill_price=avg_d,
        fee=fee,
        timestamp=_iso_now(),
    )


class KrakenAdapter(BrokerAdapter):
    """
    Kraken spot REST adapter. No native paper trading: ``paper_mode=True`` skips
    real order placement (returns a rejected placeholder) but still allows
    public reads and authenticated balance queries if API keys are set.
    """

    broker_name = "kraken"

    _HEALTH_CHECK_INTERVAL = 60.0

    def __init__(
        self,
        api_key: str = "",
        api_secret: str = "",
        paper_mode: bool = True,
        **kwargs: Any,
    ) -> None:
        _ = kwargs
        self.api_key = api_key or ""
        self.api_secret = api_secret or ""
        self.paper_mode = paper_mode
        self._lock = asyncio.Lock()
        self._connected = False
        self._private_ok = False
        self._market: Any = None
        self._user: Any = None
        self._trade: Any = None
        self._last_health_check: float = 0.0
        self._health_ok: bool = False

    async def _run_sync(self, fn: Callable[[], T]) -> T:
        async with self._lock:
            return await asyncio.to_thread(fn)

    def _require_private(self) -> None:
        if not self._private_ok or self._user is None or self._trade is None:
            raise RuntimeError("Kraken private API not available (connect with API keys)")

    async def connect(self) -> bool:
        for attempt in range(2):
            try:
                return await self._try_connect()
            except Exception as exc:  # noqa: BLE001
                err_str = str(exc).lower()
                is_rate_limit = "rate limit" in err_str or "eapi:rate limit" in err_str
                if is_rate_limit:
                    logger.warning(
                        "connect | Kraken | rate limited — will retry via reconnect loop",
                    )
                    self._connected = False
                    self._private_ok = False
                    return False
                is_nonce = "nonce" in err_str
                if is_nonce and attempt == 0:
                    logger.warning("connect | Kraken | nonce error — retrying in 2s")
                    await asyncio.sleep(2)
                    continue
                logger.exception("connect | Kraken | failed | error={}", exc)
                self._connected = False
                self._private_ok = False
                return False
        return False

    async def _try_connect(self) -> bool:
        Market, Trade, User = _kraken_sdk_classes()
        self._market = Market()
        await self._run_sync(lambda: self._market.get_system_status())  # type: ignore[union-attr]

        if self.api_key and self.api_secret:
            self._user = User(key=self.api_key, secret=self.api_secret)
            self._trade = Trade(key=self.api_key, secret=self.api_secret)
            await self._run_sync(lambda: self._user.get_account_balance())  # type: ignore[union-attr]
            self._private_ok = True
            logger.info("connect | Kraken | private API | ok")
        else:
            self._user = None
            self._trade = None
            self._private_ok = False
            if not self.paper_mode:
                logger.error("connect | Kraken | live mode requires KRAKEN_API_KEY and KRAKEN_API_SECRET")
                self._connected = False
                return False
            logger.info("connect | Kraken | public only (no API keys)")

        self._connected = True
        self._health_ok = True
        self._last_health_check = time.monotonic()
        logger.info(
            "connect | Kraken | paper_mode={} | private={}",
            self.paper_mode,
            self._private_ok,
        )
        return True

    async def disconnect(self) -> None:
        self._connected = False
        self._private_ok = False
        self._market = None
        self._user = None
        self._trade = None
        logger.info("disconnect | Kraken | done")

    async def is_connected(self) -> bool:
        if not self._connected or self._market is None:
            return False
        now = time.monotonic()
        if now - self._last_health_check < self._HEALTH_CHECK_INTERVAL:
            return self._health_ok
        self._last_health_check = now
        try:
            await self._run_sync(lambda: self._market.get_system_status())
            self._health_ok = True
            return True
        except Exception:
            logger.warning("is_connected | Kraken | health check failed — marking disconnected")
            self._connected = False
            self._health_ok = False
            return False

    async def get_balance(self) -> list[Balance]:
        if not self._private_ok or self._user is None:
            return []

        def _fetch_pair() -> tuple[dict[str, Any], dict[str, Any]]:
            assert self._user is not None
            basic = self._user.get_account_balance()
            try:
                extended = self._user.get_balances()
            except Exception:  # noqa: BLE001 — BalanceEx optional / shape varies
                extended = {}
            return basic, extended

        raw_basic, raw_ex = await self._run_sync(_fetch_pair)
        holds: dict[str, Decimal] = {}
        for asset, row in raw_ex.items():
            if isinstance(row, dict):
                holds[str(asset)] = _d(row.get("hold_trade", "0"))
            elif row is not None:
                holds[str(asset)] = Decimal(0)

        out: list[Balance] = []
        seen: set[str] = set()
        for asset, bal_s in raw_basic.items():
            a = str(asset)
            seen.add(a)
            total = _d(bal_s)
            hold = holds.get(a, Decimal(0))
            avail = total - hold
            if total == 0 and hold == 0:
                continue
            out.append(
                Balance(
                    currency=a,
                    total=total,
                    available=avail if avail >= 0 else Decimal(0),
                    reserved=hold,
                )
            )
        for asset, row in raw_ex.items():
            a = str(asset)
            if a in seen:
                continue
            if not isinstance(row, dict):
                continue
            total = _d(row.get("balance", "0"))
            hold = _d(row.get("hold_trade", "0"))
            avail = total - hold
            if total == 0 and hold == 0:
                continue
            out.append(
                Balance(
                    currency=a,
                    total=total,
                    available=avail if avail >= 0 else Decimal(0),
                    reserved=hold,
                )
            )
        if not out:
            if raw_basic:
                logger.info(
                    "get_balance | Kraken | {} row(s) from Balance API, all zero — "
                    "spot wallet has no usable funds (check Kraken → Funding)",
                    len(raw_basic),
                )
            else:
                logger.info(
                    "get_balance | Kraken | Balance API returned no asset rows "
                    "(empty portfolio for this key / sub-account)"
                )
        return out

    async def get_positions(self) -> list[Position]:
        return []

    async def place_order(self, order: Order) -> OrderResult:
        if order.order_type in (OrderType.STOP, OrderType.STOP_LIMIT):
            raise ValueError("Kraken spot adapter: STOP / STOP_LIMIT not implemented")

        pair = _pair_altname(order.symbol)
        side = "buy" if order.side == OrderSide.BUY else "sell"
        tif = (order.time_in_force or "GTC").upper()

        if self.paper_mode:
            logger.warning(
                "place_order | Kraken | paper_mode | not sending live order | pair={} side={} qty={}",
                pair,
                side,
                order.quantity,
            )
            return OrderResult(
                broker_order_id="",
                client_order_id=order.client_order_id,
                status=OrderStatus.REJECTED,
                symbol=order.symbol,
                side=order.side,
                quantity=order.quantity,
                filled_quantity=Decimal(0),
                avg_fill_price=None,
                fee=None,
                timestamp=_iso_now(),
            )

        self._require_private()
        uref = _userref_from_client_id(order.client_order_id)

        def _existing_open() -> OrderResult | None:
            assert self._user is not None
            oo = self._user.get_open_orders(userref=uref) if uref is not None else {}
            for txid, od in oo.get("open", {}).items():
                return _order_dict_to_result(txid, od, order.symbol)
            return None

        if uref is not None:
            existing = await self._run_sync(_existing_open)
            if existing is not None:
                logger.info("place_order | Kraken | idempotent hit | txid={}", existing.broker_order_id)
                return replace(existing, client_order_id=order.client_order_id)

        def _submit() -> dict[str, Any]:
            assert self._trade is not None
            kw: dict[str, Any] = {
                "pair": pair,
                "side": side,
                "volume": str(order.quantity),
                "validate": False,
            }
            if uref is not None:
                kw["userref"] = uref
            if order.order_type == OrderType.MARKET:
                # Kraken market orders do not use time-in-force the same way as limits.
                return self._trade.create_order(ordertype="market", **kw)
            if order.order_type == OrderType.LIMIT:
                if order.limit_price is None:
                    raise ValueError("limit_price required for LIMIT")
                kw["timeinforce"] = tif
                return self._trade.create_order(
                    ordertype="limit",
                    price=str(order.limit_price),
                    **kw,
                )
            raise ValueError(f"Unsupported order type: {order.order_type}")

        try:
            resp = await self._run_sync(_submit)
            txids = resp.get("txid") or []
            if not txids:
                raise RuntimeError(f"Kraken AddOrder unexpected response: {resp}")
            txid = str(txids[0])
            return await self.get_order(txid)
        except Exception as exc:  # noqa: BLE001
            logger.exception("place_order | Kraken | error={}", exc)
            return OrderResult(
                broker_order_id="",
                client_order_id=order.client_order_id,
                status=OrderStatus.REJECTED,
                symbol=order.symbol,
                side=order.side,
                quantity=order.quantity,
                filled_quantity=Decimal(0),
                avg_fill_price=None,
                fee=None,
                timestamp=_iso_now(),
            )

    async def cancel_order(self, broker_order_id: str) -> bool:
        self._require_private()
        try:
            r = await self._run_sync(
                lambda: self._trade.cancel_order(txid=broker_order_id)  # type: ignore[union-attr]
            )
            return int(r.get("count", 0)) > 0
        except Exception as exc:  # noqa: BLE001
            logger.warning("cancel_order | Kraken | txid={} | error={}", broker_order_id, exc)
            return False

    async def get_order(self, broker_order_id: str) -> OrderResult:
        self._require_private()
        raw = await self._run_sync(
            lambda: self._user.get_orders_info(txid=broker_order_id)  # type: ignore[union-attr]
        )
        if broker_order_id not in raw:
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
        return _order_dict_to_result(broker_order_id, raw[broker_order_id], "")

    async def get_open_orders(self) -> list[OrderResult]:
        if not self._private_ok or self._user is None:
            return []
        raw = await self._run_sync(lambda: self._user.get_open_orders())
        return [
            _order_dict_to_result(txid, od, "")
            for txid, od in raw.get("open", {}).items()
        ]

    async def get_candles(self, symbol: str, timeframe: str, limit: int = 200) -> list[Candle]:
        if self._market is None:
            return []
        interval = _OHLC_INTERVAL_MINUTES.get(timeframe)
        if interval is None:
            raise ValueError(f"Unsupported timeframe: {timeframe}")
        pair = _pair_altname(symbol)

        def _fetch() -> dict[str, Any]:
            assert self._market is not None
            return self._market.get_ohlc(pair=pair, interval=interval)

        raw = await self._run_sync(_fetch)
        _, rows = _unwrap_first_pair_value(raw)
        out: list[Candle] = []
        for row in rows[-limit:]:
            ts_u, op, hi, lo, cl, vwap, vol, _n = row[0], row[1], row[2], row[3], row[4], row[5], row[6], row[7]
            ts = datetime.fromtimestamp(int(ts_u), tz=timezone.utc).replace(microsecond=0)
            ts_s = ts.isoformat().replace("+00:00", "Z")
            out.append(
                Candle(
                    symbol=symbol,
                    timestamp=ts_s,
                    open=_d(op),
                    high=_d(hi),
                    low=_d(lo),
                    close=_d(cl),
                    volume=_d(vol),
                    timeframe=timeframe,
                )
            )
        return out

    async def get_order_book(self, symbol: str, depth: int = 10) -> OrderBook:
        if self._market is None:
            raise RuntimeError("not connected")
        pair = _pair_altname(symbol)
        count = max(1, min(depth, 500))

        def _fetch() -> dict[str, Any]:
            assert self._market is not None
            return self._market.get_order_book(pair=pair, count=count)

        raw = await self._run_sync(_fetch)
        _, book = _unwrap_first_pair_value(raw)
        bids: list[tuple[Decimal, Decimal]] = []
        asks: list[tuple[Decimal, Decimal]] = []
        for price_s, vol_s, *_ in book.get("bids", [])[:depth]:
            bids.append((_d(price_s), _d(vol_s)))
        for price_s, vol_s, *_ in book.get("asks", [])[:depth]:
            asks.append((_d(price_s), _d(vol_s)))
        return OrderBook(symbol=symbol, timestamp=_iso_now(), bids=bids, asks=asks)

    async def get_last_price(self, symbol: str) -> Decimal:
        if self._market is None:
            return Decimal(0)
        pair = _pair_altname(symbol)

        def _fetch() -> dict[str, Any]:
            assert self._market is not None
            return self._market.get_ticker(pair=pair)

        raw = await self._run_sync(_fetch)
        _, tick = _unwrap_first_pair_value(raw)
        last = (tick.get("c") or ["0"])[0]
        return _d(last)

    async def stream_prices(self, symbols: list[str]) -> AsyncIterator[Tick]:
        if not symbols:
            return
        while self._connected:
            for sym in symbols:
                if not self._connected:
                    break
                try:
                    px = await self.get_last_price(sym)
                    ob = await self.get_order_book(sym, depth=1)
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
                    logger.warning("stream_prices | Kraken | symbol={} | error={}", sym, exc)
            await asyncio.sleep(1.0)

    async def get_supported_symbols(self) -> list[str]:
        if self._market is None:
            return []

        def _fetch() -> dict[str, Any]:
            assert self._market is not None
            return self._market.get_asset_pairs()

        raw = await self._run_sync(_fetch)
        names: list[str] = []
        for _k, info in raw.items():
            ws = info.get("wsname")
            if ws:
                names.append(str(ws))
            else:
                names.append(str(info.get("altname", "")))
        return sorted({n for n in names if n})

    async def get_asset_class(self, symbol: str) -> AssetClass:
        return AssetClass.CRYPTO
