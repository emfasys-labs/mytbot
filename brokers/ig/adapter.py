"""
brokers/ig/adapter.py
=====================
IG Markets REST API adapter (spread bet / CFD — equities, indices, FX, commodities).

Docs: https://labs.ig.com/rest-trading-api-reference.html
Demo: ``https://demo-api.ig.com/gateway/deal``
Live: ``https://api.ig.com/gateway/deal``

Auth: ``POST /session`` (Version 2) with ``X-IG-API-KEY`` plus JSON
``identifier`` (IG username) and ``password`` (IG account password).
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

_DEMO_BASE = "https://demo-api.ig.com/gateway/deal"
_LIVE_BASE = "https://api.ig.com/gateway/deal"
_SESSION_TTL_SEC = 5 * 3600.0  # refresh before 6-hour idle expiry
_API_VERSION = "2"


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


def _epic_to_canonical(epic: str, *, instrument_name: str = "") -> str:
    raw = (instrument_name or epic or "").strip().upper()
    if not raw:
        return raw
    if raw.endswith("=X") or raw.endswith("=F"):
        return raw
    if len(raw) == 6 and raw.isalpha():
        return f"{raw}=X"
    if "." in epic:
        parts = epic.upper().split(".")
        if len(parts) >= 3:
            token = parts[2]
            if token in {"EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "USDCHF"}:
                return f"{token}=X"
            if token in {"US500", "NAS100", "FTSE", "DAX"}:
                if token == "US500":
                    return "ES=F"
                if token == "NAS100":
                    return "NQ=F"
            if len(token) <= 6 and token.isalpha() and token not in {"CFD", "CASH", "DAILY"}:
                return token
    return raw


def _ig_auth_hint(status_code: int, body: str, *, paper_mode: bool) -> str:
    env = "demo (IG_PAPER_MODE=true)" if paper_mode else "live (IG_PAPER_MODE=false)"
    if "error.security.api-key-invalid" in body:
        return (
            f"API key rejected on {env}. IG keys are environment-specific — use a demo key "
            "with demo-api.ig.com or a live key with api.ig.com."
        )
    if "validation.pattern.invalid.authenticationRequest.identifier" in body:
        return (
            "IG_IDENTIFIER must be your Web API username from My IG → Settings → "
            "API keys (Demo/Live Web API login), not your email."
        )
    if "error.security.invalid-details" in body:
        return "IG username/password rejected — use the Web API login from API settings, not MyIG login."
    if status_code in {401, 403}:
        return f"IG auth failed on {env}; verify API key, Web API username, and password match the same environment."
    return ""


def _guess_search_term(symbol: str) -> str:
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
            return "NAS100"
        if root == "CL":
            return "OIL"
        if root == "GC":
            return "GOLD"
        return root
    if sym.endswith(".L"):
        return sym[:-2]
    return sym


class IGAdapter(BrokerAdapter):
    """IG Markets adapter (REST, httpx, CST session tokens)."""

    broker_name = "ig"

    def __init__(
        self,
        api_key: str = "",
        password: str = "",
        identifier: str = "",
        paper_mode: bool = True,
        base_url: str | None = None,
        **kwargs: Any,
    ) -> None:
        _ = kwargs
        self.api_key = (api_key or os.getenv("IG_API_KEY", "")).strip()
        self.password = (password or os.getenv("IG_PASSWORD", "")).strip()
        self.identifier = (
            (identifier or os.getenv("IG_IDENTIFIER", "")).strip()
            or os.getenv("IG_USERNAME", "").strip()
            or os.getenv("IG_LOGIN", "").strip()
        )
        env_paper = os.getenv("IG_PAPER_MODE", "").strip().lower()
        if env_paper:
            self.paper_mode = env_paper in {"1", "true", "yes", "on"}
        else:
            self.paper_mode = paper_mode
        self.base_url = (base_url or "").strip() or None
        self._rest_gap = AsyncRestGap.from_env("IG", default_seconds=1.05)
        self._client: httpx.AsyncClient | None = None
        self._connected = False
        self._cst: str | None = None
        self._security_token: str | None = None
        self._account_id: str | None = None
        self._session_last_used = 0.0
        self._currency = "GBP"
        self._epic_by_canonical: dict[str, str] = {}
        self._canonical_by_epic: dict[str, str] = {}
        self._last_prices: dict[str, Decimal] = {}
        self._deal_id_by_epic: dict[str, str] = {}

    def _resolve_base_url(self) -> str:
        if self.base_url:
            return self.base_url.rstrip("/")
        return _DEMO_BASE if self.paper_mode else _LIVE_BASE

    def _auth_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {
            "Accept": "application/json; charset=UTF-8",
            "Content-Type": "application/json",
            "Version": _API_VERSION,
        }
        if self.api_key:
            headers["X-IG-API-KEY"] = self.api_key
        if self._cst:
            headers["CST"] = self._cst
        if self._security_token:
            headers["X-SECURITY-TOKEN"] = self._security_token
        if self._account_id:
            headers["IG-ACCOUNT-ID"] = self._account_id
        return headers

    async def _create_session(self) -> None:
        if self._client is None:
            raise RuntimeError("IG client not connected")
        if not self.api_key or not self.password or not self.identifier:
            raise RuntimeError(
                "IG requires IG_API_KEY, IG_IDENTIFIER (username), and IG_PASSWORD"
            )
        await self._rest_gap.wait()
        resp = await self._client.post(
            f"{self._resolve_base_url()}/session",
            headers={
                "X-IG-API-KEY": self.api_key,
                "Accept": "application/json; charset=UTF-8",
                "Content-Type": "application/json",
                "Version": _API_VERSION,
            },
            json={"identifier": self.identifier, "password": self.password},
        )
        if resp.status_code >= 400:
            body = resp.text[:500]
            hint = _ig_auth_hint(resp.status_code, body, paper_mode=self.paper_mode)
            detail = f"IG session → {resp.status_code}: {body}"
            if hint:
                detail = f"{detail} | hint: {hint}"
            raise httpx.HTTPStatusError(
                detail,
                request=resp.request,
                response=resp,
            )
        self._cst = resp.headers.get("CST") or resp.headers.get("cst")
        self._security_token = resp.headers.get("X-SECURITY-TOKEN") or resp.headers.get(
            "x-security-token"
        )
        if not self._cst or not self._security_token:
            raise RuntimeError("IG session missing CST / X-SECURITY-TOKEN headers")
        payload = resp.json() if resp.content else {}
        if isinstance(payload, dict):
            acct = payload.get("currentAccountId") or payload.get("accountId")
            if acct:
                self._account_id = str(acct)
            ccy = payload.get("currency") or payload.get("currencyIsoCode")
            if ccy:
                self._currency = str(ccy).upper()
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
        version: str | None = None,
    ) -> Any:
        if self._client is None:
            raise RuntimeError("IG client not connected")
        await self._ensure_session()
        await self._rest_gap.wait()
        rel = path if path.startswith("/") else f"/{path}"
        url = f"{self._resolve_base_url()}{rel}"
        headers = self._auth_headers()
        if version is not None:
            headers["Version"] = version
        resp = await self._client.request(
            method,
            url,
            headers=headers,
            json=json_body,
            params=params,
        )
        if resp.status_code in {401, 403} and retry_auth:
            await self._create_session()
            await self._rest_gap.wait()
            headers = self._auth_headers()
            if version is not None:
                headers["Version"] = version
            resp = await self._client.request(
                method,
                url,
                headers=headers,
                json=json_body,
                params=params,
            )
        if resp.status_code >= 400:
            body = resp.text[:500]
            raise httpx.HTTPStatusError(
                f"IG {method} {path} → {resp.status_code}: {body}",
                request=resp.request,
                response=resp,
            )
        self._session_last_used = time.monotonic()
        if resp.status_code == 204 or not resp.content:
            return None
        return resp.json()

    def _canonical_symbol(self, epic: str, *, instrument_name: str = "") -> str:
        key = (epic or "").strip().upper()
        if key in self._canonical_by_epic:
            return self._canonical_by_epic[key]
        return _epic_to_canonical(key, instrument_name=instrument_name)

    async def _resolve_epic(self, symbol: str) -> str:
        sym = (symbol or "").strip().upper()
        if not sym:
            return sym
        if sym in self._epic_by_canonical:
            return self._epic_by_canonical[sym]
        if "." in sym and sym.count(".") >= 2:
            self._epic_by_canonical[sym] = sym
            self._canonical_by_epic[sym] = _epic_to_canonical(sym)
            return sym
        guess = _guess_search_term(sym)
        try:
            raw = await self._request("GET", "/markets", params={"searchTerm": guess}, version="1")
            markets = raw.get("markets") if isinstance(raw, dict) else []
            if isinstance(markets, list):
                for row in markets:
                    if not isinstance(row, dict):
                        continue
                    epic = str(row.get("epic") or "").strip()
                    name = str(row.get("instrumentName") or epic).strip()
                    if not epic:
                        continue
                    epic_up = epic.upper()
                    name_up = name.upper()
                    if (
                        epic_up == sym
                        or guess in epic_up
                        or guess in name_up
                        or sym in name_up
                    ):
                        self._epic_by_canonical[sym] = epic
                        self._canonical_by_epic[epic_up] = _epic_to_canonical(
                            epic, instrument_name=name
                        )
                        return epic
                if markets:
                    first = markets[0]
                    if isinstance(first, dict) and first.get("epic"):
                        epic = str(first["epic"])
                        name = str(first.get("instrumentName") or epic)
                        self._epic_by_canonical[sym] = epic
                        self._canonical_by_epic[epic.upper()] = _epic_to_canonical(
                            epic, instrument_name=name
                        )
                        return epic
        except Exception as exc:  # noqa: BLE001
            logger.debug("ig | market search failed for {}: {}", sym, exc)
        self._epic_by_canonical[sym] = guess
        self._canonical_by_epic[guess.upper()] = _epic_to_canonical(guess)
        return guess

    def _is_reduce_only(self, order: Order) -> bool:
        md = order.instrument_metadata if isinstance(order.instrument_metadata, dict) else {}
        return bool(md.get("reduce_only") or md.get("close_only"))

    async def connect(self) -> bool:
        if not self.api_key:
            logger.error("ig | missing IG_API_KEY")
            self._connected = False
            return False
        if not self.identifier:
            logger.error("ig | missing IG_IDENTIFIER (IG username)")
            self._connected = False
            return False
        if not self.password:
            logger.error("ig | missing IG_PASSWORD (IG account password)")
            self._connected = False
            return False
        try:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(30.0))
            await self._create_session()
            accounts = await self._request("GET", "/accounts", version="1")
            if isinstance(accounts, dict):
                for row in accounts.get("accounts") or []:
                    if not isinstance(row, dict):
                        continue
                    if row.get("preferred") or len(accounts.get("accounts") or []) == 1:
                        bal = row.get("balance") if isinstance(row.get("balance"), dict) else {}
                        self._currency = str(row.get("currency") or self._currency).upper()
                        if row.get("accountId"):
                            self._account_id = str(row["accountId"])
                        logger.info(
                            "connect | IG | ok | env={} currency={} available={}",
                            "demo" if self.paper_mode else "live",
                            self._currency,
                            bal.get("available"),
                        )
                        break
            self._connected = True
            logger.info(
                "connect | IG | ok | env={} currency={}",
                "demo" if self.paper_mode else "live",
                self._currency,
            )
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error("connect | IG | failed | {}", exc)
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
        raw = await self._request("GET", "/accounts", version="1")
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
        raw = await self._request("GET", "/positions", version="2")
        if not isinstance(raw, dict):
            return []
        out: list[Position] = []
        self._deal_id_by_epic.clear()
        for row in raw.get("positions") or []:
            if not isinstance(row, dict):
                continue
            pos = row.get("position") if isinstance(row.get("position"), dict) else {}
            mkt = row.get("market") if isinstance(row.get("market"), dict) else {}
            epic = str(mkt.get("epic") or "").strip()
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
            inst_name = str(mkt.get("instrumentName") or "")
            sym = self._canonical_symbol(epic, instrument_name=inst_name)
            if cur > 0:
                self._last_prices[sym] = cur
            deal_id = str(pos.get("dealId") or "")
            if deal_id:
                self._deal_id_by_epic[epic.upper()] = deal_id
            inst_type = str(mkt.get("instrumentType") or "").upper()
            ac = AssetClass.FUTURE
            if inst_type in {"SHARES", "EQUITIES", "SHARE"}:
                ac = AssetClass.EQUITY
            elif inst_type in {"ETF", "ETFS", "INDICES"}:
                ac = AssetClass.ETF if inst_type != "INDICES" else AssetClass.FUTURE
            elif inst_type in {"CURRENCIES", "FX", "CURRENCY"}:
                ac = AssetClass.FOREX
            out.append(
                Position(
                    symbol=sym,
                    asset_class=ac,
                    quantity=abs(qty),
                    avg_entry_price=level,
                    current_price=cur,
                    unrealised_pnl=_d(pos.get("upl")),
                    broker=self.broker_name,
                    instrument_metadata={
                        "epic": epic,
                        "deal_id": deal_id,
                        "direction": direction,
                    },
                )
            )
        return out

    async def _confirm_deal(self, deal_reference: str) -> dict[str, Any]:
        raw = await self._request("GET", f"/confirms/{deal_reference}", version="1")
        return raw if isinstance(raw, dict) else {}

    async def place_order(self, order: Order) -> OrderResult:
        epic = await self._resolve_epic(order.symbol)
        qty = abs(_d(order.quantity))
        if qty <= 0:
            return self._reject(order, qty)
        reduce_only = self._is_reduce_only(order)
        epic_key = epic.upper()
        if reduce_only:
            deal_id = self._deal_id_by_epic.get(epic_key)
            close_direction = "SELL"
            if not deal_id:
                for pos in await self.get_positions():
                    md = pos.instrument_metadata or {}
                    if str(md.get("epic") or "").upper() == epic_key:
                        deal_id = str(md.get("deal_id") or "")
                        held_dir = str(md.get("direction") or "BUY").upper()
                        close_direction = "SELL" if held_dir == "BUY" else "BUY"
                        break
            if deal_id:
                try:
                    raw = await self._request(
                        "DELETE",
                        "/positions/otc",
                        json_body={
                            "dealId": deal_id,
                            "direction": close_direction,
                            "size": float(qty),
                            "orderType": "MARKET",
                            "timeInForce": "FILL_OR_KILL",
                        },
                        version="2",
                    )
                    ref = ""
                    if isinstance(raw, dict):
                        ref = str(raw.get("dealReference") or "")
                    confirm = await self._confirm_deal(ref) if ref else {}
                    status = str(confirm.get("dealStatus") or confirm.get("status") or "ACCEPTED").upper()
                    level = _d(confirm.get("level"))
                    return OrderResult(
                        broker_order_id=deal_id,
                        client_order_id=order.client_order_id,
                        status=OrderStatus.FILLED if status == "ACCEPTED" else OrderStatus.PENDING,
                        symbol=self._canonical_symbol(epic),
                        side=order.side,
                        quantity=qty,
                        filled_quantity=qty if status == "ACCEPTED" else Decimal(0),
                        avg_fill_price=level if level > 0 else None,
                        fee=None,
                        timestamp=_iso_now(),
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.error("ig | close {} failed: {}", epic, exc)
                    return self._reject(order, qty)
        direction = "BUY" if order.side == OrderSide.BUY else "SELL"
        if order.order_type == OrderType.LIMIT and order.limit_price is not None:
            return self._reject(order, qty)
        payload: dict[str, Any] = {
            "epic": epic,
            "expiry": "-",
            "direction": direction,
            "size": float(qty),
            "orderType": "MARKET",
            "guaranteedStop": False,
            "forceOpen": True,
            "currencyCode": self._currency,
        }
        try:
            raw = await self._request("POST", "/positions/otc", json_body=payload, version="2")
        except Exception as exc:  # noqa: BLE001
            logger.error("ig | place_order {} {} failed: {}", order.symbol, direction, exc)
            return self._reject(order, qty)
        deal_ref = str((raw or {}).get("dealReference") or "") if isinstance(raw, dict) else ""
        confirm = await self._confirm_deal(deal_ref) if deal_ref else {}
        affected = confirm.get("affectedDeals") if isinstance(confirm, dict) else []
        deal_id = deal_ref
        if isinstance(affected, list) and affected:
            first = affected[0]
            if isinstance(first, dict) and first.get("dealId"):
                deal_id = str(first["dealId"])
        status_raw = str(confirm.get("dealStatus") or confirm.get("status") or "ACCEPTED").upper()
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
            await self._request("DELETE", f"/workingorders/otc/{broker_order_id}")
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("ig | cancel_order {} failed: {}", broker_order_id, exc)
            return False

    async def get_order(self, broker_order_id: str) -> OrderResult:
        raw = await self._request("GET", f"/workingorders/otc/{broker_order_id}")
        if not isinstance(raw, dict):
            return self._reject(
                Order(symbol="", side=OrderSide.BUY, order_type=OrderType.MARKET, quantity=Decimal(0)),
                Decimal(0),
            )
        wo = raw.get("workingOrderData") if isinstance(raw.get("workingOrderData"), dict) else raw
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
            wo = row.get("workingOrderData") if isinstance(row.get("workingOrderData"), dict) else row
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
            f"/prices/{epic}/{resolution}/{max(1, min(limit, 1000))}",
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
                    close=_d(close.get("bid") or close_.get("ask")),
                    volume=_d(bar.get("lastTradedVolume")),
                )
            )
        return out

    async def get_order_book(self, symbol: str, depth: int = 10) -> OrderBook:
        _ = depth
        epic = await self._resolve_epic(symbol)
        sym = self._canonical_symbol(epic)
        try:
            raw = await self._request("GET", f"/markets/{epic}", version="2")
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
