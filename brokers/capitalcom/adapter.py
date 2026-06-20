"""
brokers/capitalcom/adapter.py
=============================
Capital.com Public API adapter (CFD — equities, indices, FX, commodities).

Docs: https://open-api.capital.com/
Demo: ``https://demo-api-capital.backend-capital.com/api/v1``
Live: ``https://api-capital.backend-capital.com/api/v1``

Auth: ``POST /session`` with ``X-CAP-API-KEY`` header plus JSON
``identifier`` (platform login) and ``password`` (API-key custom password).
Subsequent calls use ``CST`` and ``X-SECURITY-TOKEN`` response headers.
"""

from __future__ import annotations

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

_DEMO_BASE = "https://demo-api-capital.backend-capital.com/api/v1"
_LIVE_BASE = "https://api-capital.backend-capital.com/api/v1"
_SESSION_TTL_SEC = 540.0  # refresh before 10-minute idle expiry


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


def _epic_to_canonical(epic: str, *, symbol: str = "") -> str:
    raw = (symbol or epic or "").strip().upper()
    if not raw:
        return raw
    if raw.endswith("=X") or raw.endswith("=F"):
        return raw
    if len(raw) == 6 and raw.isalpha():
        return f"{raw}=X"
    return raw


def _guess_epic(symbol: str) -> str:
    sym = (symbol or "").strip().upper()
    if not sym:
        return sym
    if sym.endswith("=X"):
        return sym[:-2]
    if sym.endswith("=F"):
        root = sym[:-2]
        if root in {"ES", "SPX"}:
            return "US500"
        if root in {"NQ", "NDX"}:
            return "US100"
        if root == "CL":
            return "OIL_CRUDE"
        if root == "GC":
            return "GOLD"
        return root
    if sym.endswith(".L"):
        return sym[:-2]
    return sym


class CapitalComAdapter(BrokerAdapter):
    """Capital.com CFD adapter (REST, httpx, session tokens)."""

    broker_name = "capitalcom"

    def __init__(
        self,
        api_key: str = "",
        api_password: str = "",
        identifier: str = "",
        paper_mode: bool = True,
        base_url: str | None = None,
        **kwargs: Any,
    ) -> None:
        _ = kwargs
        self.api_key = (api_key or os.getenv("CAPITALCOM_API_KEY", "")).strip()
        self.api_password = (
            (api_password or os.getenv("CAPITALCOM_API_PASSWORD", "")).strip()
        )
        self.identifier = (
            (identifier or os.getenv("CAPITALCOM_IDENTIFIER", "")).strip()
            or os.getenv("CAPITALCOM_LOGIN", "").strip()
            or os.getenv("CAPITALCOM_EMAIL", "").strip()
        )
        env_paper = os.getenv("CAPITALCOM_PAPER_MODE", "").strip().lower()
        if env_paper:
            self.paper_mode = env_paper in {"1", "true", "yes", "on"}
        else:
            self.paper_mode = paper_mode
        self.base_url = (base_url or "").strip() or None
        self._rest_gap = AsyncRestGap.from_env("CAPITALCOM", default_seconds=1.05)
        self._client: httpx.AsyncClient | None = None
        self._connected = False
        self._cst: str | None = None
        self._security_token: str | None = None
        self._session_last_used = 0.0
        self._currency = "USD"
        self._epic_by_canonical: dict[str, str] = {}
        self._canonical_by_epic: dict[str, str] = {}
        self._last_prices: dict[str, Decimal] = {}
        self._deal_id_by_epic: dict[str, str] = {}

    def _resolve_base_url(self) -> str:
        if self.base_url:
            return self.base_url.rstrip("/")
        return _DEMO_BASE if self.paper_mode else _LIVE_BASE

    def _auth_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {"Accept": "application/json"}
        if self.api_key:
            headers["X-CAP-API-KEY"] = self.api_key
        if self._cst:
            headers["CST"] = self._cst
        if self._security_token:
            headers["X-SECURITY-TOKEN"] = self._security_token
        return headers

    async def _create_session(self) -> None:
        if self._client is None:
            raise RuntimeError("Capital.com client not connected")
        if not self.api_key or not self.api_password or not self.identifier:
            raise RuntimeError(
                "Capital.com requires CAPITALCOM_API_KEY, CAPITALCOM_API_PASSWORD, "
                "and CAPITALCOM_IDENTIFIER (platform login email)"
            )
        await self._rest_gap.wait()
        resp = await self._client.post(
            f"{self._resolve_base_url()}/session",
            headers={"X-CAP-API-KEY": self.api_key, "Accept": "application/json"},
            json={
                "identifier": self.identifier,
                "password": self.api_password,
                "encryptedPassword": False,
            },
        )
        if resp.status_code >= 400:
            body = resp.text[:500]
            raise httpx.HTTPStatusError(
                f"Capital.com session → {resp.status_code}: {body}",
                request=resp.request,
                response=resp,
            )
        self._cst = resp.headers.get("CST") or resp.headers.get("cst")
        self._security_token = resp.headers.get("X-SECURITY-TOKEN") or resp.headers.get(
            "x-security-token"
        )
        if not self._cst or not self._security_token:
            raise RuntimeError("Capital.com session missing CST / X-SECURITY-TOKEN headers")
        payload = resp.json() if resp.content else {}
        if isinstance(payload, dict) and payload.get("currency"):
            self._currency = str(payload["currency"]).upper()
        self._session_last_used = time.monotonic()

    async def _ensure_session(self, *, force: bool = False) -> None:
        if force or not self._cst or not self._security_token:
            await self._create_session()
            return
        if time.monotonic() - self._session_last_used >= _SESSION_TTL_SEC:
            await self._create_session()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        retry_auth: bool = True,
    ) -> Any:
        if self._client is None:
            raise RuntimeError("Capital.com client not connected")
        await self._ensure_session()
        await self._rest_gap.wait()
        rel = path if path.startswith("/") else f"/{path}"
        url = f"{self._resolve_base_url()}{rel}"
        resp = await self._client.request(
            method,
            url,
            headers=self._auth_headers(),
            json=json_body,
            params=params,
        )
        if resp.status_code in {401, 403} and retry_auth:
            await self._create_session()
            await self._rest_gap.wait()
            resp = await self._client.request(
                method,
                url,
                headers=self._auth_headers(),
                json=json_body,
                params=params,
            )
        if resp.status_code >= 400:
            body = resp.text[:500]
            raise httpx.HTTPStatusError(
                f"Capital.com {method} {path} → {resp.status_code}: {body}",
                request=resp.request,
                response=resp,
            )
        self._session_last_used = time.monotonic()
        if resp.status_code == 204 or not resp.content:
            return None
        return resp.json()

    def _canonical_symbol(self, epic: str, *, symbol: str = "") -> str:
        key = (epic or "").strip().upper()
        if key in self._canonical_by_epic:
            return self._canonical_by_epic[key]
        return _epic_to_canonical(key, symbol=symbol)

    async def _resolve_epic(self, symbol: str) -> str:
        sym = (symbol or "").strip().upper()
        if not sym:
            return sym
        if sym in self._epic_by_canonical:
            return self._epic_by_canonical[sym]
        guess = _guess_epic(sym)
        try:
            raw = await self._request("GET", "/markets", params={"searchTerm": guess})
            markets = raw.get("markets") if isinstance(raw, dict) else []
            if isinstance(markets, list):
                for row in markets:
                    if not isinstance(row, dict):
                        continue
                    epic = str(row.get("epic") or "").strip().upper()
                    m_sym = str(row.get("symbol") or epic).strip().upper()
                    if epic == sym or m_sym == sym or epic == guess or m_sym == guess:
                        self._epic_by_canonical[sym] = epic
                        self._canonical_by_epic[epic] = _epic_to_canonical(epic, symbol=m_sym)
                        return epic
        except Exception as exc:  # noqa: BLE001
            logger.debug("capitalcom | market search failed for {}: {}", sym, exc)
        self._epic_by_canonical[sym] = guess
        self._canonical_by_epic[guess] = _epic_to_canonical(guess)
        return guess

    def _is_reduce_only(self, order: Order) -> bool:
        md = order.instrument_metadata if isinstance(order.instrument_metadata, dict) else {}
        return bool(md.get("reduce_only") or md.get("close_only"))

    async def connect(self) -> bool:
        if not self.api_key or not self.api_password:
            logger.error("capitalcom | missing CAPITALCOM_API_KEY / CAPITALCOM_API_PASSWORD")
            self._connected = False
            return False
        if not self.identifier:
            logger.error(
                "capitalcom | missing CAPITALCOM_IDENTIFIER (Capital.com login email)"
            )
            self._connected = False
            return False
        try:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(30.0))
            await self._create_session()
            accounts = await self._request("GET", "/accounts")
            if isinstance(accounts, dict):
                for row in accounts.get("accounts") or []:
                    if not isinstance(row, dict):
                        continue
                    if row.get("preferred"):
                        bal = row.get("balance") if isinstance(row.get("balance"), dict) else {}
                        self._currency = str(row.get("currency") or self._currency).upper()
                        if bal:
                            logger.info(
                                "connect | Capital.com | ok | env={} currency={} available={}",
                                "demo" if self.paper_mode else "live",
                                self._currency,
                                bal.get("available"),
                            )
                        break
            self._connected = True
            logger.info(
                "connect | Capital.com | ok | env={} currency={}",
                "demo" if self.paper_mode else "live",
                self._currency,
            )
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error("connect | Capital.com | failed | {}", exc)
            if self._client is not None:
                await self._client.aclose()
                self._client = None
            self._connected = False
            return False

    async def disconnect(self) -> None:
        if self._client is not None and self._cst and self._security_token:
            try:
                await self._client.delete(
                    f"{self._resolve_base_url()}/session",
                    headers=self._auth_headers(),
                )
            except Exception:  # noqa: BLE001
                pass
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        self._connected = False
        self._cst = None
        self._security_token = None

    async def is_connected(self) -> bool:
        return self._connected and self._client is not None

    async def get_balance(self) -> list[Balance]:
        raw = await self._request("GET", "/accounts")
        if not isinstance(raw, dict):
            return []
        out: list[Balance] = []
        for row in raw.get("accounts") or []:
            if not isinstance(row, dict):
                continue
            bal = row.get("balance") if isinstance(row.get("balance"), dict) else {}
            total = _d(bal.get("balance"))
            available = _d(bal.get("available"))
            deposit = _d(bal.get("deposit"))
            reserved = max(Decimal(0), deposit - available) if deposit > 0 else max(
                Decimal(0), total - available
            )
            ccy = str(row.get("currency") or self._currency).upper()
            out.append(
                Balance(
                    currency=ccy,
                    total=total,
                    available=available,
                    reserved=reserved,
                )
            )
        return out

    async def get_positions(self) -> list[Position]:
        raw = await self._request("GET", "/positions")
        if not isinstance(raw, dict):
            return []
        out: list[Position] = []
        self._deal_id_by_epic.clear()
        for row in raw.get("positions") or []:
            if not isinstance(row, dict):
                continue
            pos = row.get("position") if isinstance(row.get("position"), dict) else {}
            mkt = row.get("market") if isinstance(row.get("market"), dict) else {}
            epic = str(mkt.get("epic") or "").strip().upper()
            if not epic:
                continue
            size = _d(pos.get("size"))
            if size <= 0:
                continue
            direction = str(pos.get("direction") or "BUY").upper()
            qty = size if direction == "BUY" else -size
            level = _d(pos.get("level"))
            bid = _d(mkt.get("bid"))
            offer = _d(mkt.get("offer"))
            cur = offer if offer > 0 else bid if bid > 0 else level
            if cur > 0:
                sym = self._canonical_symbol(epic, symbol=str(mkt.get("symbol") or ""))
                self._last_prices[sym] = cur
            deal_id = str(pos.get("dealId") or "")
            if deal_id:
                self._deal_id_by_epic[epic] = deal_id
            inst_type = str(mkt.get("instrumentType") or "").upper()
            ac = AssetClass.FUTURE
            if inst_type in {"SHARES", "EQUITIES"}:
                ac = AssetClass.EQUITY
            elif inst_type in {"ETF", "ETFS"}:
                ac = AssetClass.ETF
            elif inst_type in {"CURRENCIES", "FX"}:
                ac = AssetClass.FOREX
            sym = self._canonical_symbol(epic, symbol=str(mkt.get("symbol") or ""))
            out.append(
                Position(
                    symbol=sym,
                    asset_class=ac,
                    quantity=abs(qty),
                    avg_entry_price=level,
                    current_price=cur,
                    unrealised_pnl=_d(pos.get("upl")),
                    broker=self.broker_name,
                    instrument_metadata={"epic": epic, "deal_id": deal_id, "direction": direction},
                )
            )
        return out

    async def _confirm_deal(self, deal_reference: str) -> dict[str, Any]:
        raw = await self._request("GET", f"/confirms/{deal_reference}")
        return raw if isinstance(raw, dict) else {}

    async def place_order(self, order: Order) -> OrderResult:
        epic = await self._resolve_epic(order.symbol)
        qty = abs(_d(order.quantity))
        if qty <= 0:
            return self._reject(order, qty)
        reduce_only = self._is_reduce_only(order)
        if reduce_only and order.side == OrderSide.SELL:
            deal_id = self._deal_id_by_epic.get(epic)
            if not deal_id:
                for pos in await self.get_positions():
                    md = pos.instrument_metadata or {}
                    if str(md.get("epic") or "").upper() == epic:
                        deal_id = str(md.get("deal_id") or "")
                        break
            if deal_id:
                try:
                    raw = await self._request("DELETE", f"/positions/{deal_id}")
                    ref = ""
                    if isinstance(raw, dict):
                        ref = str(raw.get("dealReference") or "")
                    confirm = await self._confirm_deal(ref) if ref else {}
                    status = str(confirm.get("status") or "ACCEPTED").upper()
                    return OrderResult(
                        broker_order_id=deal_id,
                        client_order_id=order.client_order_id,
                        status=OrderStatus.FILLED if status == "ACCEPTED" else OrderStatus.PENDING,
                        symbol=self._canonical_symbol(epic),
                        side=OrderSide.SELL,
                        quantity=qty,
                        filled_quantity=qty,
                        avg_fill_price=None,
                        fee=None,
                        timestamp=_iso_now(),
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.error("capitalcom | close {} failed: {}", epic, exc)
                    return self._reject(order, qty)
        direction = "BUY" if order.side == OrderSide.BUY else "SELL"
        payload: dict[str, Any] = {
            "epic": epic,
            "direction": direction,
            "size": float(qty),
        }
        if order.order_type == OrderType.LIMIT and order.limit_price is not None:
            return self._reject(order, qty)  # v1: market opens only
        try:
            raw = await self._request("POST", "/positions", json_body=payload)
        except Exception as exc:  # noqa: BLE001
            logger.error("capitalcom | place_order {} {} failed: {}", order.symbol, direction, exc)
            return self._reject(order, qty)
        deal_ref = str((raw or {}).get("dealReference") or "") if isinstance(raw, dict) else ""
        confirm = await self._confirm_deal(deal_ref) if deal_ref else {}
        affected = confirm.get("affectedDeals") if isinstance(confirm, dict) else []
        deal_id = deal_ref
        if isinstance(affected, list) and affected:
            first = affected[0]
            if isinstance(first, dict) and first.get("dealId"):
                deal_id = str(first["dealId"])
        status_raw = str(confirm.get("status") or "ACCEPTED").upper()
        level = _d(confirm.get("level"))
        if level <= 0 and isinstance(affected, list) and affected:
            level = _d(affected[0].get("level") if isinstance(affected[0], dict) else 0)
        return OrderResult(
            broker_order_id=deal_id,
            client_order_id=order.client_order_id,
            status=OrderStatus.FILLED if status_raw == "ACCEPTED" else OrderStatus.PENDING,
            symbol=self._canonical_symbol(epic),
            side=order.side,
            quantity=qty,
            filled_quantity=qty if status_raw == "ACCEPTED" else Decimal(0),
            avg_fill_price=level if level > 0 else None,
            fee=None,
            timestamp=_iso_now(),
        )

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

    async def cancel_order(self, broker_order_id: str) -> bool:
        try:
            await self._request("DELETE", f"/workingorders/{broker_order_id}")
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("capitalcom | cancel_order {} failed: {}", broker_order_id, exc)
            return False

    async def get_order(self, broker_order_id: str) -> OrderResult:
        raw = await self._request("GET", f"/workingorders/{broker_order_id}")
        if not isinstance(raw, dict):
            return self._reject(Order(symbol="", side=OrderSide.BUY, order_type=OrderType.MARKET, quantity=Decimal(0)), Decimal(0))
        wo = raw.get("workingOrder") if isinstance(raw.get("workingOrder"), dict) else raw
        epic = str(wo.get("epic") or "")
        direction = str(wo.get("direction") or "BUY").upper()
        side = OrderSide.BUY if direction == "BUY" else OrderSide.SELL
        return OrderResult(
            broker_order_id=broker_order_id,
            client_order_id=None,
            status=OrderStatus.OPEN,
            symbol=self._canonical_symbol(epic),
            side=side,
            quantity=_d(wo.get("size")),
            filled_quantity=Decimal(0),
            avg_fill_price=_d(wo.get("level")) or None,
            fee=None,
            timestamp=_iso_now(),
        )

    async def get_open_orders(self) -> list[OrderResult]:
        raw = await self._request("GET", "/workingorders")
        if not isinstance(raw, dict):
            return []
        out: list[OrderResult] = []
        for row in raw.get("workingOrders") or []:
            if not isinstance(row, dict):
                continue
            wo = row.get("workingOrder") if isinstance(row.get("workingOrder"), dict) else row
            deal_id = str(wo.get("dealId") or wo.get("dealReference") or "")
            if deal_id:
                out.append(await self.get_order(deal_id))
        return out

    async def get_candles(self, symbol: str, timeframe: str, limit: int = 200) -> list[Candle]:
        epic = await self._resolve_epic(symbol)
        resolution = "DAY"
        tf = (timeframe or "").strip().lower()
        if tf in {"1h", "60m", "hour"}:
            resolution = "HOUR"
        elif tf in {"1m", "minute"}:
            resolution = "MINUTE"
        raw = await self._request(
            "GET",
            f"/prices/{epic}",
            params={"resolution": resolution, "max": max(1, min(limit, 1000))},
        )
        if not isinstance(raw, dict):
            return []
        prices = raw.get("prices") if isinstance(raw.get("prices"), list) else []
        out: list[Candle] = []
        for bar in prices:
            if not isinstance(bar, dict):
                continue
            close = bar.get("closePrice") if isinstance(bar.get("closePrice"), dict) else {}
            open_ = bar.get("openPrice") if isinstance(bar.get("openPrice"), dict) else {}
            high = bar.get("highPrice") if isinstance(bar.get("highPrice"), dict) else {}
            low = bar.get("lowPrice") if isinstance(bar.get("lowPrice"), dict) else {}
            out.append(
                Candle(
                    symbol=self._canonical_symbol(epic),
                    timestamp=str(bar.get("snapshotTimeUTC") or bar.get("snapshotTime") or _iso_now()),
                    open=_d(open_.get("bid") or open_.get("ask")),
                    high=_d(high.get("bid") or high.get("ask")),
                    low=_d(low.get("bid") or low.get("ask")),
                    close=_d(close.get("bid") or close.get("ask")),
                    volume=_d(bar.get("lastTradedVolume")),
                )
            )
        return out

    async def get_order_book(self, symbol: str, depth: int = 10) -> OrderBook:
        _ = depth
        epic = await self._resolve_epic(symbol)
        sym = self._canonical_symbol(epic)
        try:
            raw = await self._request("GET", f"/markets/{epic}")
        except Exception:  # noqa: BLE001
            raw = None
        bid = offer = Decimal(0)
        if isinstance(raw, dict):
            snap = raw.get("snapshot") if isinstance(raw.get("snapshot"), dict) else {}
            bid = _d(snap.get("bid"))
            offer = _d(snap.get("offer"))
        if bid <= 0 and offer <= 0:
            cached = self._last_prices.get(sym, Decimal(0))
            if cached > 0:
                bid = offer = cached
        if bid <= 0 and offer <= 0:
            return OrderBook(symbol=sym, timestamp=_iso_now(), bids=[], asks=[])
        return OrderBook(
            symbol=sym,
            timestamp=_iso_now(),
            bids=[(bid, Decimal(0))],
            asks=[(offer, Decimal(0))],
        )

    async def get_last_price(self, symbol: str) -> Decimal:
        sym = (symbol or "").strip().upper()
        cached = self._last_prices.get(sym)
        if cached and cached > 0:
            return cached
        book = await self.get_order_book(sym)
        if book.asks:
            return book.asks[0][0]
        if book.bids:
            return book.bids[0][0]
        return Decimal(0)

    async def stream_prices(self, symbols: list[str]) -> AsyncIterator[Tick]:
        _ = symbols
        if False:
            yield Tick(symbol="", timestamp=_iso_now(), price=Decimal(0), volume=Decimal(0), bid=None, ask=None)

    async def get_supported_symbols(self) -> list[str]:
        return sorted(set(self._canonical_by_epic.values()))

    async def get_asset_class(self, symbol: str) -> AssetClass:
        sym = (symbol or "").strip().upper()
        if sym.endswith("=X"):
            return AssetClass.FOREX
        if sym.endswith("=F"):
            return AssetClass.FUTURE
        if sym in {"SPY", "QQQ", "IWM", "VTI", "VOO", "GLD", "TLT"}:
            return AssetClass.ETF
        return AssetClass.EQUITY
