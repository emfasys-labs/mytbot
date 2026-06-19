"""
brokers/bybit/adapter.py
========================
Bybit V5 unified trading (spot or USDT linear perps).

SDK: pybit
Docs: https://bybit-exchange.github.io/docs/v5/intro
"""

from __future__ import annotations

import asyncio
import os
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
from brokers.rest_rate_limit import AsyncRestGap

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
    """BTC/USDT -> BTCUSDT; canonical BTC-USD uses the USDT venue book."""
    s = symbol.strip().upper().replace(" ", "").replace("/", "-")
    if s.endswith("-USD"):
        s = f"{s[:-4]}-USDT"
    return s.replace("-", "")


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
        rest_iv = kwargs.pop("rest_min_interval_sec", None)
        _ = kwargs
        self.api_key = (api_key or "").strip()
        self.api_secret = (api_secret or "").strip()
        self.paper_mode = paper_mode
        self.testnet = testnet
        self.category = (category or "linear").strip().lower()
        if self.category not in ("spot", "linear"):
            raise ValueError("Bybit category must be 'spot' or 'linear'")
        if rest_iv is not None:
            self._rest_gap = AsyncRestGap(float(rest_iv))
        else:
            self._rest_gap = AsyncRestGap.from_env("BYBIT", default_seconds=0.05)
        try:
            self._recv_window_ms = max(5000, int(os.getenv("BYBIT_RECV_WINDOW_MS", "10000")))
        except (TypeError, ValueError):
            self._recv_window_ms = 10000
        self._lock = asyncio.Lock()
        self._connected = False
        self._private_ok = False
        self._client: HTTP | None = None
        self._timestamp_offset_ms: int = 0
        self._order_symbol: dict[str, str] = {}
        self._wallet_account_type: str | None = None
        self._wallet_unavailable: bool = False
        # Symbols Bybit reports as invalid (ErrCode 10001) — typically equities
        # probed against the crypto venue. Cache them so we (a) stop hammering
        # the API with doomed requests and (b) don't spam thousands of identical
        # WARNING lines that bury real issues.
        self._invalid_symbols: set[str] = set()
        # Circuit breaker for get_positions. Some Bybit account configurations
        # (no UTA / linear permission missing) reject get_positions with ErrCode
        # 400 every call. Without a breaker we log 700+ identical warnings/hour
        # and burn the pybit retry budget on each invocation. After
        # ``_positions_breaker_threshold`` consecutive failures we suppress the
        # call for ``_positions_breaker_cooldown`` seconds; the breaker resets on
        # the first successful response.
        self._positions_fail_count: int = 0
        self._positions_breaker_until: float = 0.0
        self._positions_breaker_threshold: int = 3
        self._positions_breaker_cooldown: float = 300.0

    def _candidate_wallet_account_types(self) -> list[str]:
        # Probe order biased by adapter category: linear perps → CONTRACT/UNIFIED first,
        # spot → SPOT/UNIFIED first. FUND is rarely the right answer so it goes last.
        if self.category == "spot":
            return ["UNIFIED", "SPOT", "CONTRACT", "FUND"]
        return ["UNIFIED", "CONTRACT", "SPOT", "FUND"]

    @staticmethod
    def _is_account_type_error(exc: Exception) -> bool:
        s = str(exc).lower()
        return "errcode: 400" in s and "accounttype" in s

    async def _detect_wallet_account_type(self) -> str | None:
        """
        Probe wallet account types with a single attempt each (no SDK retry storm).

        Returns the first working type, or ``None`` if every probe rejects the key
        (which usually means the key lacks wallet-read permission). In that case the
        caller should mark ``_wallet_unavailable=True`` so we stop retrying.
        """
        if self._client is None:
            return None
        for account_type in self._candidate_wallet_account_types():
            try:
                await self._run_sync(
                    lambda at=account_type: self._client.get_wallet_balance(accountType=at)  # type: ignore[union-attr]
                )
                return account_type
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "Bybit | wallet probe failed | accountType={} | {}",
                    account_type,
                    exc,
                )
                continue
        return None

    async def _run_sync(self, fn: Callable[[], T]) -> T:
        async with self._lock:
            await self._rest_gap.wait()
            if not self._timestamp_offset_ms:
                return await asyncio.to_thread(fn)

            import pybit._helpers as pybit_helpers

            original_generate_timestamp = pybit_helpers.generate_timestamp

            def _offset_timestamp() -> int:
                return int(original_generate_timestamp()) + self._timestamp_offset_ms

            pybit_helpers.generate_timestamp = _offset_timestamp
            try:
                return await asyncio.to_thread(fn)
            finally:
                pybit_helpers.generate_timestamp = original_generate_timestamp

    def _require_private(self) -> None:
        if not self._private_ok or self._client is None:
            raise RuntimeError("Bybit private API not available (connect with API keys)")

    async def connect(self) -> bool:
        try:
            # Keep the pybit retry window short so a transient 4xx/5xx cannot
            # stall us past the broker-manager startup timeout.
            self._client = HTTP(
                testnet=self.testnet,
                api_key=self.api_key or None,
                api_secret=self.api_secret or None,
                timeout=10,
                max_retries=1,
                retry_delay=1,
                recv_window=self._recv_window_ms,
            )
            # Public — always available.
            server_time = await self._run_sync(lambda: self._client.get_server_time())  # type: ignore[union-attr]
            try:
                result = (server_time.get("result", {}) or {}) if isinstance(server_time, dict) else {}
                raw_ms = result.get("timeNano") or result.get("timeSecond")
                server_ms = int(raw_ms)
                if "timeNano" in result:
                    server_ms = server_ms // 1_000_000
                else:
                    server_ms = server_ms * 1000
                local_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
                self._timestamp_offset_ms = server_ms - local_ms
                if abs(self._timestamp_offset_ms) > 500:
                    logger.warning(
                        "connect | Bybit | timestamp skew detected | offset_ms={} | signing private requests with server-time offset",
                        self._timestamp_offset_ms,
                    )
            except Exception as exc:  # noqa: BLE001
                self._timestamp_offset_ms = 0
                logger.debug("connect | Bybit | server-time offset unavailable | {}", exc)

            if self.api_key and self.api_secret:
                # Validate credentials with a cheap authenticated endpoint that works on
                # both Classic and UTA accounts. This avoids the slow wallet-balance
                # probe storm that used to blow past the 15s startup window.
                try:
                    await self._run_sync(
                        lambda: self._client.get_api_key_information()  # type: ignore[union-attr]
                    )
                    self._private_ok = True
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "connect | Bybit | API key validation failed | error={}", exc,
                    )
                    self._private_ok = False
                    if not self.paper_mode:
                        self._connected = False
                        self._client = None
                        return False

                logger.info(
                    "connect | Bybit | private API | private_ok={} | testnet={} | category={}",
                    self._private_ok,
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
        # Synthetic paper wallet (no Bybit-native paper account): in paper
        # mode report ledger-derived venue equity so crypto P&L flows into
        # NAV. CRYPTO_PAPER_WALLET=0 disables.
        if self.paper_mode:
            from system.paper_wallet import venue_equity

            eq = venue_equity("bybit")
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
        if self._wallet_unavailable:
            return []

        if self._wallet_account_type is None:
            detected = await self._detect_wallet_account_type()
            if detected is None:
                self._wallet_unavailable = True
                logger.warning(
                    "get_balance | Bybit | wallet balance endpoint rejected every accountType "
                    "(likely API key lacks wallet-read permission or account is restricted) — "
                    "treating wallet as unavailable; adapter stays connected for price/order data.",
                )
                return []
            self._wallet_account_type = detected
            logger.info(
                "Bybit | wallet accountType resolved | accountType={}",
                self._wallet_account_type,
            )

        def _fetch() -> dict[str, Any]:
            assert self._client is not None
            return self._client.get_wallet_balance(accountType=self._wallet_account_type)

        try:
            raw = await self._run_sync(_fetch)
        except Exception as exc:  # noqa: BLE001
            # If this was an accountType 400, reset the cached type so the next call retries
            # the probe. If probing fails again get_balance will latch into _wallet_unavailable.
            if self._is_account_type_error(exc):
                self._wallet_account_type = None
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

        now = asyncio.get_event_loop().time()
        if now < self._positions_breaker_until:
            return []

        def _fetch() -> dict[str, Any]:
            assert self._client is not None
            return self._client.get_positions(category="linear", settleCoin="USDT")

        try:
            raw = await self._run_sync(_fetch)
        except Exception as exc:  # noqa: BLE001
            self._positions_fail_count += 1
            if self._positions_fail_count >= self._positions_breaker_threshold:
                self._positions_breaker_until = now + self._positions_breaker_cooldown
                logger.warning(
                    "get_positions | Bybit | breaker tripped after {} failures | suppressing for {:.0f}s | last_error={}",
                    self._positions_fail_count,
                    self._positions_breaker_cooldown,
                    exc,
                )
            else:
                logger.warning(
                    "get_positions | Bybit | error ({}/{}) | {}",
                    self._positions_fail_count,
                    self._positions_breaker_threshold,
                    exc,
                )
            return []

        self._positions_fail_count = 0
        self._positions_breaker_until = 0.0

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
            meta = dict(order.instrument_metadata or {})
            meta.setdefault("error_message", "Bybit has no native paper order placement in this adapter; order was not sent")
            meta.setdefault("reject_reason", "paper_mode_no_native_order")
            meta.setdefault("rejected_by", "bybit")
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
            meta = dict(order.instrument_metadata or {})
            meta.setdefault("error_message", str(msg)[:500])
            meta.setdefault("reject_reason", str(msg)[:200])
            meta.setdefault("rejected_by", "bybit")
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
        except Exception as exc:  # noqa: BLE001
            logger.exception("place_order | Bybit | error={}", exc)
            meta = dict(order.instrument_metadata or {})
            meta.setdefault("error_message", str(exc)[:500])
            meta.setdefault("reject_reason", str(exc)[:200])
            meta.setdefault("rejected_by", "bybit")
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
        # Known-invalid (e.g. an equity ticker on the crypto venue): don't even
        # issue the doomed request again.
        if sym in self._invalid_symbols:
            return Decimal(0)

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
            msg = str(exc).lower()
            if isinstance(exc, InvalidRequestError) or "symbol invalid" in msg or (
                "errcode: 10001" in msg
            ):
                # Benign + expected: this symbol simply isn't tradable on Bybit.
                # Cache it and log once at debug instead of warning every cycle.
                if sym not in self._invalid_symbols:
                    self._invalid_symbols.add(sym)
                    logger.debug(
                        "get_last_price | Bybit | symbol not on venue, "
                        "suppressing further probes | symbol={}",
                        sym,
                    )
            else:
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

    async def _fetch_symbol_rules(self, symbol: str) -> dict[str, Decimal]:
        symbol = _bybit_symbol(symbol)
        if not hasattr(self, "_symbol_rules_cache"):
            self._symbol_rules_cache = {}
        if symbol in self._symbol_rules_cache:
            return self._symbol_rules_cache[symbol]

        rules = {"qty_step": Decimal("0.0001"), "tick_size": Decimal("0.01")}
        if self._client is None:
            return rules

        def _go():
            assert self._client is not None
            return self._client.get_instruments_info(category=self.category, symbol=symbol)

        try:
            raw = await self._run_sync(_go)
            lst = (raw.get("result", {}) or {}).get("list") or []
            if lst:
                info = lst[0]
                pf = info.get("priceFilter", {})
                ts = pf.get("tickSize")
                if ts:
                    rules["tick_size"] = Decimal(str(ts))
                lf = info.get("lotSizeFilter", {})
                qs = lf.get("qtyStep")
                if qs:
                    rules["qty_step"] = Decimal(str(qs))
                else:
                    bp = lf.get("basePrecision")
                    if bp:
                        rules["qty_step"] = Decimal(str(bp))
            self._symbol_rules_cache[symbol] = rules
        except Exception as exc:
            logger.debug("_fetch_symbol_rules | Bybit | symbol={} | error={}", symbol, exc)

        return rules

    async def quantize_quantity(self, symbol: str, quantity: Decimal) -> Decimal:
        rules = await self._fetch_symbol_rules(symbol)
        step = rules["qty_step"]
        try:
            q = (quantity / step).quantize(Decimal("1"), rounding=ROUND_DOWN) * step
            return q.normalize()
        except Exception:
            return quantity

    async def quantize_price(self, symbol: str, price: Decimal, side: Optional[OrderSide] = None) -> Decimal:
        rules = await self._fetch_symbol_rules(symbol)
        tick = rules["tick_size"]
        try:
            p = (price / tick).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * tick
            return p.normalize()
        except Exception:
            return price
