"""
brokers/coinbase/adapter.py
===========================
Coinbase Advanced Trade adapter (crypto spot, CDP API key + EC private key JWT).

Docs: https://docs.cdp.coinbase.com/coinbase-app/advanced-trade-apis/rest-api
Auth: ES256 JWT per request (``Authorization: Bearer``).
"""

from __future__ import annotations

import os
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
from brokers.coinbase.auth import build_rest_jwt, normalize_pem_secret
from brokers.rest_rate_limit import AsyncRestGap

_BASE_URL = "https://api.coinbase.com"
_BROKERAGE = "/api/v3/brokerage"
_FIAT_SKIP = frozenset({"USD", "USDC", "USDT", "GBP", "EUR", "DAI", "BUSD"})


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


def _product_id(symbol: str) -> str:
    sym = (symbol or "").strip().upper().replace(" ", "")
    if not sym:
        return sym
    if sym.endswith("-USD"):
        return sym
    if sym.endswith("USDT") and len(sym) > 4:
        return f"{sym[:-4]}-USD"
    if sym.endswith("/USD"):
        return sym.replace("/", "-")
    if "-" not in sym and sym.isalpha():
        return f"{sym}-USD"
    return sym.replace("/", "-")


def _canonical_product(product_id: str) -> str:
    pid = (product_id or "").strip().upper()
    if pid.endswith("-USD"):
        return pid
    if pid.endswith("-USDT"):
        return f"{pid[:-5]}-USD"
    return pid


def _money_value(row: dict[str, Any] | None) -> tuple[Decimal, str]:
    if not isinstance(row, dict):
        return Decimal(0), "USD"
    return _d(row.get("value")), str(row.get("currency") or "USD").upper()


class CoinbaseAdapter(BrokerAdapter):
    """Coinbase Advanced Trade REST adapter (crypto spot)."""

    broker_name = "coinbase"

    def __init__(
        self,
        api_key: str = "",
        api_secret: str = "",
        paper_mode: bool = True,
        base_url: str | None = None,
        **kwargs: Any,
    ) -> None:
        _ = kwargs
        self.api_key = (api_key or os.getenv("COINBASE_API_KEY", "")).strip()
        self.api_secret = normalize_pem_secret(
            api_secret or os.getenv("COINBASE_API_SECRET", "")
        )
        self.paper_mode = paper_mode
        self.base_url = (base_url or os.getenv("COINBASE_BASE_URL", "")).strip() or _BASE_URL
        self._rest_gap = AsyncRestGap.from_env("COINBASE", default_seconds=0.12)
        self._client: httpx.AsyncClient | None = None
        self._connected = False
        self._private_ok = False
        self._last_prices: dict[str, Decimal] = {}
        self._product_ids: set[str] = set()

    def _path(self, rel: str) -> str:
        r = rel if rel.startswith("/") else f"/{rel}"
        if r.startswith(_BROKERAGE):
            return r
        return f"{_BROKERAGE}{r}"

    async def _request(
        self,
        method: str,
        rel: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        auth: bool = True,
    ) -> Any:
        if self._client is None:
            raise RuntimeError("Coinbase client not connected")
        await self._rest_gap.wait()
        path = self._path(rel)
        url = f"{self.base_url.rstrip('/')}{path}"
        headers: dict[str, str] = {"Accept": "application/json"}
        if auth:
            if not self.api_key or not self.api_secret:
                raise RuntimeError("Coinbase private request requires COINBASE_API_KEY and COINBASE_API_SECRET")
            token = build_rest_jwt(self.api_key, self.api_secret, method, path)
            headers["Authorization"] = f"Bearer {token}"
        if json_body is not None:
            headers["Content-Type"] = "application/json"
        resp = await self._client.request(method, url, headers=headers, params=params, json=json_body)
        if resp.status_code >= 400:
            body = resp.text[:500]
            raise httpx.HTTPStatusError(
                f"Coinbase {method} {path} → {resp.status_code}: {body}",
                request=resp.request,
                response=resp,
            )
        if resp.status_code == 204 or not resp.content:
            return None
        return resp.json()

    async def connect(self) -> bool:
        if not self.api_key or not self.api_secret:
            logger.error("coinbase | missing COINBASE_API_KEY / COINBASE_API_SECRET")
            self._connected = False
            return False
        try:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(30.0))
            raw = await self._request("GET", "/accounts")
            accounts = raw.get("accounts") if isinstance(raw, dict) else []
            count = len(accounts) if isinstance(accounts, list) else 0
            self._private_ok = True
            self._connected = True
            logger.info("connect | Coinbase | ok | accounts={} paper_mode={}", count, self.paper_mode)
            try:
                products = await self._request("GET", "/market/products", params={"limit": 250})
                rows = products.get("products") if isinstance(products, dict) else []
                if isinstance(rows, list):
                    for row in rows:
                        if isinstance(row, dict) and row.get("product_id"):
                            self._product_ids.add(str(row["product_id"]).upper())
            except Exception as exc:  # noqa: BLE001
                logger.debug("coinbase | product catalogue prefetch skipped: {}", exc)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error("connect | Coinbase | failed | {}", exc)
            if self._client is not None:
                await self._client.aclose()
                self._client = None
            self._connected = False
            self._private_ok = False
            return False

    async def disconnect(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        self._connected = False
        self._private_ok = False

    async def is_connected(self) -> bool:
        return self._connected and self._client is not None and self._private_ok

    async def get_balance(self) -> list[Balance]:
        if self.paper_mode:
            from system.paper_wallet import venue_equity

            eq = venue_equity("coinbase")
            if eq is not None:
                return [Balance(currency="USD", total=eq, available=eq, reserved=Decimal(0))]
        raw = await self._request("GET", "/accounts")
        if not isinstance(raw, dict):
            return []
        out: list[Balance] = []
        for row in raw.get("accounts") or []:
            if not isinstance(row, dict):
                continue
            avail, ccy = _money_value(row.get("available_balance"))
            hold, _ = _money_value(row.get("hold"))
            total = avail + hold
            if total <= 0:
                continue
            out.append(
                Balance(
                    currency=ccy,
                    total=total,
                    available=avail,
                    reserved=hold,
                )
            )
        return out

    async def get_positions(self) -> list[Position]:
        if self.paper_mode:
            from system.paper_wallet import venue_equity

            if venue_equity("coinbase") is not None:
                return []
        raw = await self._request("GET", "/accounts")
        if not isinstance(raw, dict):
            return []
        out: list[Position] = []
        for row in raw.get("accounts") or []:
            if not isinstance(row, dict):
                continue
            ccy = str(row.get("currency") or "").upper()
            if not ccy or ccy in _FIAT_SKIP:
                continue
            avail, _ = _money_value(row.get("available_balance"))
            hold, _ = _money_value(row.get("hold"))
            qty = avail + hold
            if qty <= 0:
                continue
            sym = _canonical_product(f"{ccy}-USD")
            price = await self.get_last_price(sym)
            out.append(
                Position(
                    symbol=sym,
                    asset_class=AssetClass.CRYPTO,
                    quantity=qty,
                    avg_entry_price=price if price > 0 else Decimal(0),
                    current_price=price if price > 0 else Decimal(0),
                    unrealised_pnl=Decimal(0),
                    broker=self.broker_name,
                    instrument_metadata={"product_id": _product_id(sym), "currency": ccy},
                )
            )
        return out

    async def place_order(self, order: Order) -> OrderResult:
        sym = _product_id(order.symbol)
        if self.paper_mode:
            logger.info(
                "place_order | Coinbase | paper_mode | not sending | symbol={} side={} qty={}",
                sym,
                order.side,
                order.quantity,
            )
            meta = dict(order.instrument_metadata or {})
            meta.setdefault("reject_reason", "paper_mode_no_native_order")
            order.instrument_metadata = meta
            return OrderResult(
                broker_order_id="",
                client_order_id=order.client_order_id,
                status=OrderStatus.REJECTED,
                symbol=_canonical_product(sym),
                side=order.side,
                quantity=order.quantity,
                filled_quantity=Decimal(0),
                avg_fill_price=None,
                fee=None,
                timestamp=_iso_now(),
            )
        if order.order_type != OrderType.MARKET:
            return self._reject(order, sym, "unsupported_order_type")
        cfg = {"market_market_ioc": {"base_size": str(order.quantity)}}
        payload: dict[str, Any] = {
            "client_order_id": order.client_order_id or f"mytbot-{int(datetime.now().timestamp())}",
            "product_id": sym,
            "side": "BUY" if order.side == OrderSide.BUY else "SELL",
            "order_configuration": cfg,
        }
        try:
            raw = await self._request("POST", "/orders", json_body=payload)
        except Exception as exc:  # noqa: BLE001
            logger.error("coinbase | place_order {} failed: {}", sym, exc)
            return self._reject(order, sym, str(exc)[:200])
        if not isinstance(raw, dict):
            return self._reject(order, sym, "empty_response")
        success = raw.get("success_response") if isinstance(raw.get("success_response"), dict) else raw
        order_id = str((success or {}).get("order_id") or raw.get("order_id") or "")
        return OrderResult(
            broker_order_id=order_id,
            client_order_id=order.client_order_id,
            status=OrderStatus.FILLED if order_id else OrderStatus.PENDING,
            symbol=_canonical_product(sym),
            side=order.side,
            quantity=order.quantity,
            filled_quantity=order.quantity if order_id else Decimal(0),
            avg_fill_price=None,
            fee=None,
            timestamp=_iso_now(),
        )

    def _reject(self, order: Order, sym: str, reason: str) -> OrderResult:
        return OrderResult(
            broker_order_id="",
            client_order_id=order.client_order_id,
            status=OrderStatus.REJECTED,
            symbol=_canonical_product(sym),
            side=order.side,
            quantity=order.quantity,
            filled_quantity=Decimal(0),
            avg_fill_price=None,
            fee=None,
            timestamp=_iso_now(),
        )

    async def cancel_order(self, broker_order_id: str) -> bool:
        try:
            await self._request(
                "POST",
                "/orders/batch_cancel",
                json_body={"order_ids": [broker_order_id]},
            )
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("coinbase | cancel_order {} failed: {}", broker_order_id, exc)
            return False

    async def get_order(self, broker_order_id: str) -> OrderResult:
        raw = await self._request("GET", f"/orders/historical/{broker_order_id}")
        if not isinstance(raw, dict):
            return self._reject(
                Order(symbol="", side=OrderSide.BUY, order_type=OrderType.MARKET, quantity=Decimal(0)),
                "",
                "not_found",
            )
        order = raw.get("order") if isinstance(raw.get("order"), dict) else raw
        sym = _canonical_product(str(order.get("product_id") or ""))
        side = OrderSide.BUY if str(order.get("side") or "").upper() == "BUY" else OrderSide.SELL
        st = str(order.get("status") or "").upper()
        status = OrderStatus.FILLED if st == "FILLED" else OrderStatus.OPEN if st == "OPEN" else OrderStatus.PENDING
        filled = _d(order.get("filled_size") or order.get("filled_base_size"))
        qty = _d(order.get("order_size") or order.get("base_size") or filled)
        return OrderResult(
            broker_order_id=broker_order_id,
            client_order_id=order.get("client_order_id"),
            status=status,
            symbol=sym,
            side=side,
            quantity=qty,
            filled_quantity=filled,
            avg_fill_price=_d(order.get("average_filled_price")) or None,
            fee=_d(order.get("total_fees")) or None,
            timestamp=_iso_now(),
        )

    async def get_open_orders(self) -> list[OrderResult]:
        raw = await self._request("GET", "/orders/historical/batch", params={"order_status": "OPEN"})
        if not isinstance(raw, dict):
            return []
        out: list[OrderResult] = []
        for row in raw.get("orders") or []:
            if not isinstance(row, dict):
                continue
            oid = str(row.get("order_id") or "")
            if oid:
                out.append(await self.get_order(oid))
        return out

    async def get_candles(self, symbol: str, timeframe: str, limit: int = 200) -> list[Candle]:
        pid = _product_id(symbol)
        gran = "ONE_HOUR"
        tf = (timeframe or "").strip().lower()
        if tf in {"1m", "minute"}:
            gran = "ONE_MINUTE"
        elif tf in {"5m"}:
            gran = "FIVE_MINUTE"
        elif tf in {"1d", "day", "daily"}:
            gran = "ONE_DAY"
        raw = await self._request(
            "GET",
            f"/market/products/{pid}/candles",
            params={"granularity": gran, "limit": max(1, min(limit, 300))},
            auth=False,
        )
        if not isinstance(raw, dict):
            return []
        candles = raw.get("candles") if isinstance(raw.get("candles"), list) else []
        sym = _canonical_product(pid)
        out: list[Candle] = []
        for bar in candles:
            if not isinstance(bar, dict):
                continue
            out.append(
                Candle(
                    symbol=sym,
                    timestamp=str(bar.get("start") or _iso_now()),
                    open=_d(bar.get("open")),
                    high=_d(bar.get("high")),
                    low=_d(bar.get("low")),
                    close=_d(bar.get("close")),
                    volume=_d(bar.get("volume")),
                )
            )
        return out

    async def get_order_book(self, symbol: str, depth: int = 10) -> OrderBook:
        pid = _product_id(symbol)
        sym = _canonical_product(pid)
        lim = max(1, min(depth, 50))
        raw = await self._request(
            "GET",
            f"/market/products/{pid}/book",
            params={"limit": lim},
            auth=False,
        )
        if not isinstance(raw, dict):
            return OrderBook(symbol=sym, timestamp=_iso_now(), bids=[], asks=[])
        book = raw.get("pricebook") if isinstance(raw.get("pricebook"), dict) else raw
        bids_raw = book.get("bids") if isinstance(book.get("bids"), list) else []
        asks_raw = book.get("asks") if isinstance(book.get("asks"), list) else []
        bids: list[tuple[Decimal, Decimal]] = []
        asks: list[tuple[Decimal, Decimal]] = []
        for row in bids_raw:
            if isinstance(row, dict):
                bids.append((_d(row.get("price")), _d(row.get("size"))))
        for row in asks_raw:
            if isinstance(row, dict):
                asks.append((_d(row.get("price")), _d(row.get("size"))))
        if not bids and not asks:
            px = await self.get_last_price(sym)
            if px > 0:
                bids = [(px, Decimal(0))]
                asks = [(px, Decimal(0))]
        return OrderBook(symbol=sym, timestamp=_iso_now(), bids=bids, asks=asks)

    async def get_last_price(self, symbol: str) -> Decimal:
        sym = _canonical_product(_product_id(symbol))
        cached = self._last_prices.get(sym)
        if cached and cached > 0:
            return cached
        pid = _product_id(sym)
        try:
            raw = await self._request("GET", f"/market/products/{pid}/ticker", auth=False)
        except Exception:  # noqa: BLE001
            raw = None
        px = Decimal(0)
        if isinstance(raw, dict):
            trades = raw.get("trades") if isinstance(raw.get("trades"), list) else []
            if trades and isinstance(trades[0], dict):
                px = _d(trades[0].get("price"))
            if px <= 0:
                px = _d(raw.get("price"))
        if px > 0:
            self._last_prices[sym] = px
        return px

    async def stream_prices(self, symbols: list[str]) -> AsyncIterator[Tick]:
        _ = symbols
        if False:
            yield Tick(symbol="", timestamp=_iso_now(), price=Decimal(0), volume=Decimal(0), bid=None, ask=None)

    async def get_supported_symbols(self) -> list[str]:
        if self._product_ids:
            return sorted({_canonical_product(p) for p in self._product_ids if p.endswith("-USD")})
        try:
            raw = await self._request("GET", "/market/products", params={"limit": 250}, auth=False)
            rows = raw.get("products") if isinstance(raw, dict) else []
            out: list[str] = []
            if isinstance(rows, list):
                for row in rows:
                    if isinstance(row, dict) and row.get("product_id"):
                        pid = str(row["product_id"]).upper()
                        if pid.endswith("-USD"):
                            out.append(_canonical_product(pid))
            return sorted(set(out))
        except Exception:  # noqa: BLE001
            return []

    async def get_asset_class(self, symbol: str) -> AssetClass:
        return AssetClass.CRYPTO
