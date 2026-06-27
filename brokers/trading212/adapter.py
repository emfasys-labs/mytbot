"""
brokers/trading212/adapter.py
==============================
Trading 212 Public API adapter (Invest / Stocks ISA — equities & ETFs).

Docs: https://docs.trading212.com/api
Paper: ``https://demo.trading212.com/api/v0`` (``paper_mode=True`` default)
Live: ``https://live.trading212.com/api/v0``

Auth: HTTP Basic — API key as username, API secret as password.
"""

from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, AsyncIterator

import httpx
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

_DEMO_BASE = "https://demo.trading212.com/api/v0"
_LIVE_BASE = "https://live.trading212.com/api/v0"


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


def _ticker_to_canonical(ticker: str) -> str:
    """``AAPL_US_EQ`` → ``AAPL`` (first segment before ``_``)."""
    raw = (ticker or "").strip().upper()
    if not raw:
        return raw
    if "_" in raw:
        return raw.split("_", 1)[0]
    return raw


def _guess_t212_ticker(symbol: str) -> str:
    """Best-effort canonical → Trading 212 ticker when instruments cache misses."""
    sym = (symbol or "").strip().upper()
    if not sym:
        return sym
    if "_" in sym and sym.endswith("_EQ"):
        return sym
    if sym.endswith(".L"):
        return f"{sym[:-2]}_GB_EQ"
    if sym.endswith(".DE"):
        return f"{sym[:-3]}_DE_EQ"
    if sym.endswith(".PA"):
        return f"{sym[:-3]}_FR_EQ"
    return f"{sym}_US_EQ"


class Trading212Adapter(BrokerAdapter):
    """Trading 212 equity adapter (REST, httpx)."""

    broker_name = "trading212"

    def __init__(
        self,
        api_key: str = "",
        api_secret: str = "",
        paper_mode: bool = True,
        base_url: str | None = None,
        **kwargs: Any,
    ) -> None:
        _ = kwargs
        self.api_key = (api_key or "").strip()
        self.api_secret = (api_secret or "").strip()
        env_paper = os.getenv("TRADING212_PAPER_MODE", "").strip().lower()
        if env_paper:
            self.paper_mode = env_paper in {"1", "true", "yes", "on"}
        else:
            self.paper_mode = paper_mode
        self.base_url = (base_url or "").strip() or None
        self._rest_gap = AsyncRestGap.from_env("TRADING212", default_seconds=5.1)
        self._client: httpx.AsyncClient | None = None
        self._connected = False
        self._currency = "GBP"
        self._ticker_by_canonical: dict[str, str] = {}
        self._canonical_by_ticker: dict[str, str] = {}
        self._last_prices: dict[str, Decimal] = {}
        self._balance_cache: tuple[float, list[Balance]] | None = None
        self._balance_lock = asyncio.Lock()
        try:
            self._balance_cache_ttl_sec = max(
                5.0,
                float(os.getenv("TRADING212_BALANCE_CACHE_TTL_SEC", "60")),
            )
        except ValueError:
            self._balance_cache_ttl_sec = 60.0

    def _resolve_base_url(self) -> str:
        if self.base_url:
            return self.base_url.rstrip("/")
        return _DEMO_BASE if self.paper_mode else _LIVE_BASE

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        if self._client is None:
            raise RuntimeError("Trading 212 client not connected")
        await self._rest_gap.wait()
        url = f"{self._resolve_base_url()}{path}"
        resp = await self._client.request(method, url, json=json_body, params=params)
        if resp.status_code >= 400:
            body = resp.text[:500]
            raise httpx.HTTPStatusError(
                f"Trading 212 {method} {path} → {resp.status_code}: {body}",
                request=resp.request,
                response=resp,
            )
        if resp.status_code == 204 or not resp.content:
            return None
        return resp.json()

    def _resolve_ticker(self, symbol: str) -> str:
        sym = (symbol or "").strip().upper()
        if not sym:
            return sym
        if sym in self._canonical_by_ticker:
            return sym
        if sym in self._ticker_by_canonical:
            return self._ticker_by_canonical[sym]
        return _guess_t212_ticker(sym)

    def _canonical_symbol(self, ticker: str) -> str:
        t = (ticker or "").strip().upper()
        return self._canonical_by_ticker.get(t, _ticker_to_canonical(t))

    async def _load_instruments(self) -> None:
        try:
            rows = await self._request("GET", "/equity/metadata/instruments")
        except Exception as exc:  # noqa: BLE001
            logger.warning("trading212 | instruments metadata unavailable: {}", exc)
            return
        if not isinstance(rows, list):
            return
        for row in rows:
            if not isinstance(row, dict):
                continue
            ticker = str(row.get("ticker") or "").strip().upper()
            if not ticker:
                continue
            short = str(row.get("shortName") or row.get("name") or "").strip().upper()
            canonical = _ticker_to_canonical(ticker)
            self._canonical_by_ticker[ticker] = canonical
            self._ticker_by_canonical[canonical] = ticker
            if short:
                self._ticker_by_canonical[short.replace(" ", "")] = ticker
        logger.info("trading212 | instruments cached | count={}", len(self._canonical_by_ticker))

    async def connect(self) -> bool:
        if not self.api_key or not self.api_secret:
            logger.error("trading212 | missing TRADING212_API_KEY / TRADING212_API_SECRET")
            self._connected = False
            return False
        try:
            self._client = httpx.AsyncClient(
                auth=(self.api_key, self.api_secret),
                timeout=httpx.Timeout(30.0),
                headers={"Accept": "application/json"},
            )
            summary = await self._request("GET", "/equity/account/summary")
            if isinstance(summary, dict):
                self._currency = str(summary.get("currency") or self._currency).upper()
                self._balance_cache = (
                    time.monotonic(),
                    self._balances_from_summary(summary),
                )
            self._connected = True
            logger.info(
                "connect | Trading 212 | ok | env={} currency={}",
                "demo" if self.paper_mode and not self.base_url else "live",
                self._currency,
            )
            await self._load_instruments()
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error("connect | Trading 212 | failed | {}", exc)
            if self._client is not None:
                await self._client.aclose()
                self._client = None
            self._connected = False
            return False

    async def disconnect(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        self._connected = False

    async def is_connected(self) -> bool:
        return self._connected and self._client is not None

    async def get_balance(self) -> list[Balance]:
        now = time.monotonic()
        cached = self._balance_cache
        if cached is not None and now - cached[0] <= self._balance_cache_ttl_sec:
            return list(cached[1])
        async with self._balance_lock:
            now = time.monotonic()
            cached = self._balance_cache
            if cached is not None and now - cached[0] <= self._balance_cache_ttl_sec:
                return list(cached[1])
            try:
                summary = await self._request("GET", "/equity/account/summary")
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 429 and cached is not None:
                    logger.warning(
                        "trading212 | balance rate limited; reusing last coherent snapshot"
                    )
                    self._balance_cache = (now, cached[1])
                    return list(cached[1])
                raise
            if not isinstance(summary, dict):
                return list(cached[1]) if cached is not None else []
            balances = self._balances_from_summary(summary)
            self._balance_cache = (now, balances)
            return list(balances)

    def _balances_from_summary(self, summary: dict[str, Any]) -> list[Balance]:
        cash = summary.get("cash") if isinstance(summary.get("cash"), dict) else {}
        available = _d(cash.get("availableToTrade") or cash.get("available") or summary.get("free"))
        total = _d(summary.get("totalValue") or summary.get("total") or available)
        reserved = max(Decimal(0), total - available)
        ccy = str(summary.get("currency") or self._currency).upper()
        return [
            Balance(
                currency=ccy,
                total=total,
                available=available,
                reserved=reserved,
            )
        ]

    async def get_positions(self) -> list[Position]:
        raw = await self._request("GET", "/equity/positions")
        if not isinstance(raw, list):
            return []
        out: list[Position] = []
        for row in raw:
            if not isinstance(row, dict):
                continue
            ticker = str(row.get("ticker") or "").strip().upper()
            if not ticker:
                continue
            qty = abs(_d(row.get("quantity")))
            if qty <= 0:
                continue
            side_qty = _d(row.get("quantity"))
            if side_qty < 0:
                qty = -qty
            avg = _d(row.get("averagePrice") or row.get("avgPrice"))
            cur = _d(row.get("currentPrice") or row.get("price") or avg)
            if cur > 0:
                self._last_prices[self._canonical_symbol(ticker)] = cur
            ppl = _d(row.get("ppl") or row.get("unrealisedPnl"))
            out.append(
                Position(
                    symbol=self._canonical_symbol(ticker),
                    asset_class=AssetClass.ETF if ticker.startswith(("SPY_", "QQQ_", "IWM_")) else AssetClass.EQUITY,
                    quantity=qty,
                    avg_entry_price=avg,
                    current_price=cur,
                    unrealised_pnl=ppl,
                    broker=self.broker_name,
                )
            )
        return out

    async def place_order(self, order: Order) -> OrderResult:
        ticker = self._resolve_ticker(order.symbol)
        qty = abs(_d(order.quantity))
        if qty <= 0:
            return OrderResult(
                broker_order_id="",
                client_order_id=order.client_order_id,
                status=OrderStatus.REJECTED,
                symbol=order.symbol,
                side=order.side,
                quantity=qty,
                filled_quantity=Decimal(0),
                avg_fill_price=None,
                fee=None,
                timestamp=_iso_now(),
            )
        signed_qty = qty if order.side == OrderSide.BUY else -qty
        try:
            if order.order_type == OrderType.MARKET:
                payload = {
                    "ticker": ticker,
                    "quantity": float(signed_qty),
                    "extendedHours": False,
                }
                raw = await self._request("POST", "/equity/orders/market", json_body=payload)
            elif order.order_type == OrderType.LIMIT and order.limit_price is not None:
                payload = {
                    "ticker": ticker,
                    "quantity": float(signed_qty),
                    "limitPrice": float(order.limit_price),
                    "timeInForce": "DAY",
                }
                raw = await self._request("POST", "/equity/orders/limit", json_body=payload)
            else:
                raise ValueError(f"unsupported order type: {order.order_type}")
        except Exception as exc:  # noqa: BLE001
            logger.error("trading212 | place_order failed | {} {} | {}", order.symbol, order.side, exc)
            return OrderResult(
                broker_order_id="",
                client_order_id=order.client_order_id,
                status=OrderStatus.REJECTED,
                symbol=order.symbol,
                side=order.side,
                quantity=qty,
                filled_quantity=Decimal(0),
                avg_fill_price=None,
                fee=None,
                timestamp=_iso_now(),
            )
        return self._translate_result(raw, fallback_symbol=order.symbol, fallback_side=order.side)

    async def cancel_order(self, broker_order_id: str) -> bool:
        try:
            await self._request("DELETE", f"/equity/orders/{broker_order_id}")
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("trading212 | cancel_order {} failed: {}", broker_order_id, exc)
            return False

    async def get_order(self, broker_order_id: str) -> OrderResult:
        raw = await self._request("GET", f"/equity/orders/{broker_order_id}")
        return self._translate_result(raw)

    async def get_open_orders(self) -> list[OrderResult]:
        raw = await self._request("GET", "/equity/orders")
        if not isinstance(raw, list):
            return []
        return [self._translate_result(row) for row in raw if isinstance(row, dict)]

    async def get_candles(self, symbol: str, timeframe: str, limit: int = 200) -> list[Candle]:
        _ = (symbol, timeframe, limit)
        return []

    async def get_order_book(self, symbol: str, depth: int = 10) -> OrderBook:
        _ = depth
        sym = (symbol or "").strip().upper()
        price = self._last_prices.get(sym, Decimal(0))
        if price <= 0:
            for pos in await self.get_positions():
                if pos.symbol.upper() == sym and pos.current_price > 0:
                    price = pos.current_price
                    break
        if price <= 0:
            return OrderBook(symbol=sym, timestamp=_iso_now(), bids=[], asks=[])
        return OrderBook(
            symbol=sym,
            timestamp=_iso_now(),
            bids=[(price, Decimal(0))],
            asks=[(price, Decimal(0))],
        )

    async def get_last_price(self, symbol: str) -> Decimal:
        sym = (symbol or "").strip().upper()
        cached = self._last_prices.get(sym)
        if cached and cached > 0:
            return cached
        for pos in await self.get_positions():
            if pos.symbol.upper() == sym and pos.current_price > 0:
                return pos.current_price
        return Decimal(0)

    async def stream_prices(self, symbols: list[str]) -> AsyncIterator[Tick]:
        _ = symbols
        if False:
            yield Tick(symbol="", timestamp=_iso_now(), price=Decimal(0), volume=Decimal(0), bid=None, ask=None)

    async def get_supported_symbols(self) -> list[str]:
        if not self._canonical_by_ticker:
            await self._load_instruments()
        return sorted(set(self._canonical_by_ticker.values()))

    async def get_asset_class(self, symbol: str) -> AssetClass:
        sym = (symbol or "").strip().upper()
        if sym in {"SPY", "QQQ", "IWM", "VTI", "VOO", "GLD", "TLT"}:
            return AssetClass.ETF
        return AssetClass.EQUITY

    def _translate_result(
        self,
        raw: dict[str, Any] | None,
        *,
        fallback_symbol: str = "",
        fallback_side: OrderSide | None = None,
    ) -> OrderResult:
        if not isinstance(raw, dict):
            return OrderResult(
                broker_order_id="",
                client_order_id=None,
                status=OrderStatus.REJECTED,
                symbol=fallback_symbol,
                side=fallback_side or OrderSide.BUY,
                quantity=Decimal(0),
                filled_quantity=Decimal(0),
                avg_fill_price=None,
                fee=None,
                timestamp=_iso_now(),
            )
        inst = raw.get("instrument") if isinstance(raw.get("instrument"), dict) else {}
        ticker = str(inst.get("ticker") or raw.get("ticker") or "").strip().upper()
        symbol = self._canonical_symbol(ticker) if ticker else fallback_symbol
        qty = _d(raw.get("quantity"))
        side = OrderSide.BUY if qty >= 0 else OrderSide.SELL
        if fallback_side is not None and qty == 0:
            side = fallback_side
        filled = abs(qty) if str(raw.get("status", "")).upper() in {"FILLED", "EXECUTED"} else Decimal(0)
        status_raw = str(raw.get("status") or raw.get("type") or "PENDING").upper()
        status_map = {
            "LOCAL": OrderStatus.OPEN,
            "PENDING": OrderStatus.PENDING,
            "OPEN": OrderStatus.OPEN,
            "FILLED": OrderStatus.FILLED,
            "EXECUTED": OrderStatus.FILLED,
            "CANCELLED": OrderStatus.CANCELLED,
            "CANCELED": OrderStatus.CANCELLED,
            "REJECTED": OrderStatus.REJECTED,
        }
        oid = str(raw.get("id") or raw.get("orderId") or "")
        avg = _d(raw.get("limitPrice") or raw.get("price"))
        if avg <= 0:
            avg = None
        return OrderResult(
            broker_order_id=oid,
            client_order_id=None,
            status=status_map.get(status_raw, OrderStatus.PENDING),
            symbol=symbol,
            side=side,
            quantity=abs(qty),
            filled_quantity=filled,
            avg_fill_price=avg,
            fee=None,
            timestamp=_iso_now(),
        )
