"""
brokers/oanda/adapter.py
========================
OANDA v20 REST API adapter (forex / CFD).

Docs: https://developer.oanda.com/rest-live-v20/introduction/
Practice: ``https://api-fxpractice.oanda.com``
Live: ``https://api-fxtrade.oanda.com``

Auth: personal access token as ``Authorization: Bearer <token>``.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
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

_PRACTICE_BASE = "https://api-fxpractice.oanda.com"
_LIVE_BASE = "https://api-fxtrade.oanda.com"
_API_PREFIX = "/v3"

_GRANULARITY = {
    "1m": "M1",
    "5m": "M5",
    "15m": "M15",
    "30m": "M30",
    "1h": "H1",
    "4h": "H4",
    "1d": "D",
    "1w": "W",
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


def _instrument_id(symbol: str) -> str:
    sym = (symbol or "").strip().upper().replace(" ", "")
    if not sym:
        return sym
    if "_" in sym and sym.count("_") == 1:
        return sym
    if sym.endswith("=X"):
        sym = sym[:-2]
    if sym.endswith("/USD"):
        sym = sym.replace("/", "")
    if len(sym) == 6 and sym.isalpha():
        return f"{sym[:3]}_{sym[3:]}"
    return sym.replace("/", "_").replace("-", "_")


def _canonical_instrument(instrument: str) -> str:
    raw = (instrument or "").strip().upper()
    if not raw:
        return raw
    if raw.endswith("=X") or raw.endswith("=F"):
        return raw
    if "_" in raw:
        base, quote = raw.split("_", 1)
        if len(base) == 3 and len(quote) == 3 and base.isalpha() and quote.isalpha():
            return f"{base}{quote}=X"
    if len(raw) == 6 and raw.isalpha():
        return f"{raw}=X"
    return raw


def resolve_oanda_paper_mode(*, paper_mode: bool = True) -> bool:
    """Resolve practice vs live endpoint (``OANDA_PAPER_MODE`` overrides ``APP_ENV``)."""
    env_paper = os.getenv("OANDA_PAPER_MODE", "").strip().lower()
    if env_paper:
        return env_paper in {"1", "true", "yes", "on"}
    return paper_mode


def resolve_oanda_credentials(
    *,
    paper_mode: bool = True,
    api_token: str | None = None,
    api_token_paper: str | None = None,
    account_id: str | None = None,
    account_id_paper: str | None = None,
) -> tuple[bool, str, str]:
    """Return ``(paper_mode, token, account_id)`` for the active environment."""
    use_paper = resolve_oanda_paper_mode(paper_mode=paper_mode)
    live_token = (
        (
            os.getenv("OANDA_API_TOKEN", "").strip()
            or os.getenv("OANDA_API_KEY", "").strip()
        )
        if api_token is None
        else str(api_token).strip()
    )
    practice_token = (
        os.getenv("OANDA_API_TOKEN_PAPER", "").strip()
        if api_token_paper is None
        else str(api_token_paper).strip()
    )
    live_account = (
        os.getenv("OANDA_ACCOUNT_ID", "").strip()
        if account_id is None
        else str(account_id).strip()
    )
    practice_account = (
        os.getenv("OANDA_ACCOUNT_ID_PAPER", "").strip()
        if account_id_paper is None
        else str(account_id_paper).strip()
    )
    if use_paper:
        token = practice_token or live_token
        acct = practice_account or live_account
    else:
        token = live_token or practice_token
        acct = live_account or practice_account
    return use_paper, token, acct


class OandaAdapter(BrokerAdapter):
    """OANDA v20 REST adapter (Bearer token, practice/live base URLs)."""

    broker_name = "oanda"

    def __init__(
        self,
        api_token: str | None = None,
        api_token_paper: str | None = None,
        account_id: str | None = None,
        account_id_paper: str | None = None,
        paper_mode: bool = True,
        base_url: str | None = None,
        **kwargs: Any,
    ) -> None:
        _ = kwargs
        # Supplying either token explicitly opts this adapter instance out of
        # ambient-token inheritance for the other environment. This makes an
        # explicit empty token a reliable way to validate/disable credentials
        # even when the long-lived process has OANDA_* variables populated.
        if api_token is not None and api_token_paper is None:
            api_token_paper = ""
        elif api_token_paper is not None and api_token is None:
            api_token = ""
        self.paper_mode, self.api_token, self.account_id = resolve_oanda_credentials(
            paper_mode=paper_mode,
            api_token=api_token,
            api_token_paper=api_token_paper,
            account_id=account_id,
            account_id_paper=account_id_paper,
        )
        self.base_url = (base_url or os.getenv("OANDA_BASE_URL", "")).strip() or None
        self._rest_gap = AsyncRestGap.from_env("OANDA", default_seconds=0.25)
        self._client: httpx.AsyncClient | None = None
        self._connected = False
        self._private_ok = False
        self._currency = "USD"
        self._instruments: set[str] = set()
        self._last_prices: dict[str, Decimal] = {}

    def _resolve_base_url(self) -> str:
        if self.base_url:
            return self.base_url.rstrip("/")
        return _PRACTICE_BASE if self.paper_mode else _LIVE_BASE

    def _auth_headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_token}",
        }

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        if self._client is None:
            raise RuntimeError("OANDA client not connected")
        if not self.api_token:
            raise RuntimeError("OANDA private request requires OANDA_API_TOKEN")
        await self._rest_gap.wait()
        rel = path if path.startswith("/") else f"/{path}"
        if not rel.startswith(_API_PREFIX):
            rel = f"{_API_PREFIX}{rel}"
        url = f"{self._resolve_base_url()}{rel}"
        resp = await self._client.request(
            method,
            url,
            headers=self._auth_headers(),
            json=json_body,
            params=params,
        )
        if resp.status_code >= 400:
            body = resp.text[:500]
            env = "practice" if self.paper_mode else "live"
            hint = ""
            if resp.status_code in {401, 403}:
                hint = (
                    f" | hint: token rejected on {env} endpoint "
                    f"({self._resolve_base_url()}); practice tokens require OANDA_PAPER_MODE=true"
                )
            raise httpx.HTTPStatusError(
                f"OANDA {method} {path} → {resp.status_code}: {body}{hint}",
                request=resp.request,
                response=resp,
            )
        if resp.status_code == 204 or not resp.content:
            return None
        return resp.json()

    async def _ensure_account_id(self) -> str:
        if self.account_id:
            return self.account_id
        raw = await self._request("GET", "/accounts")
        accounts = raw.get("accounts") if isinstance(raw, dict) else []
        if not isinstance(accounts, list) or not accounts:
            raise RuntimeError("OANDA /accounts returned no accounts for this token")
        preferred_tags = ("CFD", "SPREAD_BETTING")
        for tag in preferred_tags:
            for row in accounts:
                if not isinstance(row, dict) or not row.get("id"):
                    continue
                tags = row.get("tags") if isinstance(row.get("tags"), list) else []
                tag_set = {str(t).upper() for t in tags}
                if tag.upper() in tag_set and "MT4" not in tag_set:
                    self.account_id = str(row["id"])
                    return self.account_id
        first = accounts[0]
        if not isinstance(first, dict) or not first.get("id"):
            raise RuntimeError("OANDA /accounts missing account id")
        self.account_id = str(first["id"])
        return self.account_id

    def _is_reduce_only(self, order: Order) -> bool:
        md = order.instrument_metadata if isinstance(order.instrument_metadata, dict) else {}
        return bool(md.get("reduce_only") or md.get("close_only"))

    def _reject(self, order: Order, qty: Decimal) -> OrderResult:
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

    def _filled(
        self,
        order: Order,
        *,
        broker_order_id: str,
        qty: Decimal,
        fill_price: Decimal | None,
        status: OrderStatus = OrderStatus.FILLED,
    ) -> OrderResult:
        return OrderResult(
            broker_order_id=broker_order_id,
            client_order_id=order.client_order_id,
            status=status,
            symbol=order.symbol,
            side=order.side,
            quantity=qty,
            filled_quantity=qty,
            avg_fill_price=fill_price,
            fee=None,
            timestamp=_iso_now(),
        )

    async def connect(self) -> bool:
        if not self.api_token:
            env = "practice" if self.paper_mode else "live"
            logger.error(
                "oanda | missing token for {} (set OANDA_API_TOKEN_PAPER or OANDA_API_TOKEN)",
                env,
            )
            self._connected = False
            return False
        try:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(30.0))
            account_id = await self._ensure_account_id()
            summary = await self._request("GET", f"/accounts/{account_id}/summary")
            acct = summary.get("account") if isinstance(summary, dict) else {}
            if isinstance(acct, dict) and acct.get("currency"):
                self._currency = str(acct["currency"]).upper()
            nav = _d(acct.get("NAV") if isinstance(acct, dict) else None)
            self._private_ok = True
            self._connected = True
            try:
                inst_raw = await self._request("GET", f"/accounts/{account_id}/instruments")
                rows = inst_raw.get("instruments") if isinstance(inst_raw, dict) else []
                if isinstance(rows, list):
                    for row in rows:
                        if isinstance(row, dict) and row.get("name"):
                            self._instruments.add(str(row["name"]).upper())
            except Exception as exc:  # noqa: BLE001
                logger.debug("oanda | instrument catalogue prefetch skipped: {}", exc)
            logger.info(
                "connect | OANDA | ok | env={} account={} currency={} nav={} instruments={}",
                "practice" if self.paper_mode else "live",
                account_id,
                self._currency,
                nav,
                len(self._instruments),
            )
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error("connect | OANDA | failed | {}", exc)
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
        account_id = await self._ensure_account_id()
        summary = await self._request("GET", f"/accounts/{account_id}/summary")
        acct = summary.get("account") if isinstance(summary, dict) else {}
        if not isinstance(acct, dict):
            return []
        nav = _d(acct.get("NAV"))
        available = _d(acct.get("marginAvailable") or acct.get("balance"))
        balance = _d(acct.get("balance"))
        reserved = max(Decimal(0), nav - available) if nav > 0 else max(Decimal(0), balance - available)
        ccy = str(acct.get("currency") or self._currency).upper()
        return [
            Balance(
                currency=ccy,
                total=nav if nav > 0 else balance,
                available=available,
                reserved=reserved,
            )
        ]

    async def get_positions(self) -> list[Position]:
        account_id = await self._ensure_account_id()
        raw = await self._request("GET", f"/accounts/{account_id}/openPositions")
        rows = raw.get("positions") if isinstance(raw, dict) else []
        out: list[Position] = []
        if not isinstance(rows, list):
            return out
        for row in rows:
            if not isinstance(row, dict):
                continue
            instrument = str(row.get("instrument") or "").upper()
            if not instrument:
                continue
            symbol = _canonical_instrument(instrument)
            long_row = row.get("long") if isinstance(row.get("long"), dict) else {}
            short_row = row.get("short") if isinstance(row.get("short"), dict) else {}
            long_units = _d(long_row.get("units"))
            short_units = _d(short_row.get("units"))
            net = long_units + short_units
            if net == 0:
                continue
            avg = _d(long_row.get("averagePrice") if net > 0 else short_row.get("averagePrice"))
            px = await self.get_last_price(symbol)
            out.append(
                Position(
                    symbol=symbol,
                    asset_class=AssetClass.FOREX,
                    quantity=net,
                    avg_entry_price=avg,
                    current_price=px if px > 0 else avg,
                    unrealised_pnl=Decimal(0),
                    broker=self.broker_name,
                )
            )
        return out

    async def place_order(self, order: Order) -> OrderResult:
        account_id = await self._ensure_account_id()
        instrument = _instrument_id(order.symbol)
        if not instrument:
            return self._reject(order, order.quantity)
        qty = await self.quantize_quantity(order.symbol, order.quantity)
        if qty <= 0:
            return self._reject(order, order.quantity)

        if self._is_reduce_only(order):
            close_body: dict[str, Any] = {}
            positions = await self.get_positions()
            current = Decimal(0)
            for pos in positions:
                if _instrument_id(pos.symbol) == instrument:
                    current = pos.quantity
                    break
            if current == 0:
                return self._reject(order, qty)
            if current > 0:
                close_body["longUnits"] = str(abs(qty)) if qty < current else "ALL"
            else:
                close_body["shortUnits"] = str(abs(qty)) if qty < abs(current) else "ALL"
            raw = await self._request(
                "PUT",
                f"/accounts/{account_id}/positions/{instrument}/close",
                json_body=close_body,
            )
            tx = None
            if isinstance(raw, dict):
                tx = raw.get("longOrderFillTransaction") or raw.get("shortOrderFillTransaction")
            fill_price = _d(tx.get("price") if isinstance(tx, dict) else None)
            fill_units = abs(_d(tx.get("units") if isinstance(tx, dict) else None)) or qty
            return self._filled(
                order,
                broker_order_id=str(tx.get("id") if isinstance(tx, dict) else ""),
                qty=fill_units,
                fill_price=fill_price if fill_price > 0 else None,
            )

        units = qty if order.side == OrderSide.BUY else -qty
        payload: dict[str, Any] = {
            "order": {
                "type": "MARKET",
                "instrument": instrument,
                "units": str(units),
                "timeInForce": "FOK",
                "positionFill": "DEFAULT",
            }
        }
        if order.order_type == OrderType.LIMIT and order.limit_price is not None:
            payload["order"] = {
                "type": "LIMIT",
                "instrument": instrument,
                "units": str(units),
                "price": str(order.limit_price),
                "timeInForce": "GTC",
                "positionFill": "DEFAULT",
            }
        raw = await self._request(
            "POST",
            f"/accounts/{account_id}/orders",
            json_body=payload,
        )
        order_create = raw.get("orderCreateTransaction") if isinstance(raw, dict) else None
        fill_tx = raw.get("orderFillTransaction") if isinstance(raw, dict) else None
        tx = fill_tx if isinstance(fill_tx, dict) else order_create
        broker_id = str((tx or {}).get("id") or (raw or {}).get("lastTransactionID") or "")
        status = OrderStatus.FILLED if isinstance(fill_tx, dict) else OrderStatus.OPEN
        fill_price = _d((fill_tx or {}).get("price") if isinstance(fill_tx, dict) else None)
        if fill_price <= 0 and order.limit_price is not None:
            fill_price = _d(order.limit_price)
        filled_qty = abs(_d((fill_tx or {}).get("units") if isinstance(fill_tx, dict) else qty))
        if filled_qty <= 0:
            filled_qty = qty
        fill_px = fill_price if fill_price > 0 else None
        return self._filled(
            order,
            broker_order_id=broker_id,
            qty=filled_qty,
            fill_price=fill_px,
            status=status,
        )

    async def cancel_order(self, broker_order_id: str) -> bool:
        account_id = await self._ensure_account_id()
        await self._request(
            "PUT",
            f"/accounts/{account_id}/orders/{broker_order_id}/cancel",
        )
        return True

    async def get_order(self, broker_order_id: str) -> OrderResult:
        account_id = await self._ensure_account_id()
        raw = await self._request("GET", f"/accounts/{account_id}/orders/{broker_order_id}")
        row = raw.get("order") if isinstance(raw, dict) else None
        if not isinstance(row, dict):
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
        state = str(row.get("state") or "").upper()
        status_map = {
            "FILLED": OrderStatus.FILLED,
            "CANCELLED": OrderStatus.CANCELLED,
            "PENDING": OrderStatus.PENDING,
            "TRIGGERED": OrderStatus.OPEN,
        }
        units = abs(_d(row.get("units")))
        return OrderResult(
            broker_order_id=broker_order_id,
            client_order_id=None,
            status=status_map.get(state, OrderStatus.PENDING),
            symbol=_canonical_instrument(str(row.get("instrument") or "")),
            side=OrderSide.BUY if _d(row.get("units")) >= 0 else OrderSide.SELL,
            quantity=units,
            filled_quantity=abs(_d(row.get("filledUnits"))),
            avg_fill_price=_d(row.get("price")) if row.get("price") else None,
            fee=None,
            timestamp=_iso_now(),
        )

    async def get_open_orders(self) -> list[OrderResult]:
        account_id = await self._ensure_account_id()
        raw = await self._request("GET", f"/accounts/{account_id}/pendingOrders")
        rows = raw.get("orders") if isinstance(raw, dict) else []
        out: list[OrderResult] = []
        if not isinstance(rows, list):
            return out
        for row in rows:
            if not isinstance(row, dict):
                continue
            oid = str(row.get("id") or "")
            if not oid:
                continue
            out.append(
                OrderResult(
                    broker_order_id=oid,
                    client_order_id=None,
                    status=OrderStatus.OPEN,
                    symbol=_canonical_instrument(str(row.get("instrument") or "")),
                    side=OrderSide.BUY if _d(row.get("units")) >= 0 else OrderSide.SELL,
                    quantity=abs(_d(row.get("units"))),
                    filled_quantity=Decimal(0),
                    avg_fill_price=_d(row.get("price")) if row.get("price") else None,
                    fee=None,
                    timestamp=_iso_now(),
                )
            )
        return out

    async def get_candles(
        self,
        symbol: str,
        timeframe: str,
        limit: int = 100,
    ) -> list[Candle]:
        instrument = _instrument_id(symbol)
        gran = _GRANULARITY.get(timeframe.lower(), "H1")
        raw = await self._request(
            "GET",
            f"/instruments/{instrument}/candles",
            params={"granularity": gran, "count": max(1, min(limit, 5000)), "price": "M"},
        )
        rows = raw.get("candles") if isinstance(raw, dict) else []
        out: list[Candle] = []
        if not isinstance(rows, list):
            return out
        for row in rows:
            if not isinstance(row, dict) or not row.get("complete"):
                continue
            mid = row.get("mid") if isinstance(row.get("mid"), dict) else {}
            ts = str(row.get("time") or "")
            try:
                ts_dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            except ValueError:
                continue
            out.append(
                Candle(
                    symbol=_canonical_instrument(instrument),
                    timestamp=ts,
                    open=_d(mid.get("o")),
                    high=_d(mid.get("h")),
                    low=_d(mid.get("l")),
                    close=_d(mid.get("c")),
                    volume=_d(row.get("volume")),
                    timeframe=timeframe,
                )
            )
        return out

    async def get_order_book(self, symbol: str, depth: int = 10) -> OrderBook:
        account_id = await self._ensure_account_id()
        instrument = _instrument_id(symbol)
        raw = await self._request(
            "GET",
            f"/accounts/{account_id}/pricing",
            params={"instruments": instrument},
        )
        rows = raw.get("prices") if isinstance(raw, dict) else []
        bids: list[tuple[Decimal, Decimal]] = []
        asks: list[tuple[Decimal, Decimal]] = []
        if isinstance(rows, list) and rows:
            row = rows[0] if isinstance(rows[0], dict) else {}
            for bid in (row.get("bids") or [])[:depth]:
                if isinstance(bid, dict):
                    bids.append((_d(bid.get("price")), _d(bid.get("liquidity") or 1)))
            for ask in (row.get("asks") or [])[:depth]:
                if isinstance(ask, dict):
                    asks.append((_d(ask.get("price")), _d(ask.get("liquidity") or 1)))
        if bids and asks:
            mid = (bids[0][0] + asks[0][0]) / Decimal(2)
            self._last_prices[_canonical_instrument(instrument)] = mid
        return OrderBook(
            symbol=_canonical_instrument(instrument),
            timestamp=_iso_now(),
            bids=bids,
            asks=asks,
        )

    async def get_last_price(self, symbol: str) -> Decimal:
        sym = _canonical_instrument(_instrument_id(symbol))
        cached = self._last_prices.get(sym)
        if cached and cached > 0:
            return cached
        book = await self.get_order_book(symbol, depth=1)
        if book.bids and book.asks:
            px = (book.bids[0][0] + book.asks[0][0]) / Decimal(2)
            self._last_prices[sym] = px
            return px
        if book.bids:
            self._last_prices[sym] = book.bids[0][0]
            return book.bids[0][0]
        if book.asks:
            self._last_prices[sym] = book.asks[0][0]
            return book.asks[0][0]
        return Decimal(0)

    async def stream_prices(self, symbols: list[str]) -> AsyncIterator[Tick]:
        if False:  # pragma: no cover - async generator stub
            yield Tick(symbol="", price=Decimal(0), timestamp=datetime.now(timezone.utc))
        raise NotImplementedError("OANDA streaming not implemented; use get_last_price")

    async def get_supported_symbols(self) -> list[str]:
        if self._instruments:
            return sorted({_canonical_instrument(x) for x in self._instruments})
        account_id = await self._ensure_account_id()
        raw = await self._request("GET", f"/accounts/{account_id}/instruments")
        rows = raw.get("instruments") if isinstance(raw, dict) else []
        out: list[str] = []
        if isinstance(rows, list):
            for row in rows:
                if isinstance(row, dict) and row.get("name"):
                    name = str(row["name"]).upper()
                    self._instruments.add(name)
                    out.append(_canonical_instrument(name))
        return sorted(set(out))

    async def get_asset_class(self, symbol: str) -> AssetClass:
        sym = _canonical_instrument(_instrument_id(symbol))
        if sym.endswith("=X"):
            return AssetClass.FOREX
        return AssetClass.UNKNOWN

    async def quantize_quantity(self, symbol: str, quantity: Decimal) -> Decimal:
        q = _d(quantity)
        if q <= 0:
            return Decimal(0)
        return q.quantize(Decimal("1"), rounding=ROUND_DOWN)

    async def quantize_price(self, symbol: str, price: Decimal, side: OrderSide | None = None) -> Decimal:
        _ = (symbol, side)
        px = _d(price)
        if px <= 0:
            return Decimal(0)
        return px.quantize(Decimal("0.00001"))
