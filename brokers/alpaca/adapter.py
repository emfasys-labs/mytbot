"""
brokers/alpaca/adapter.py
==========================
Alpaca adapter — US equities, ETFs, crypto (Trading API + Market Data API).

SDK: alpaca-py
Docs: https://docs.alpaca.markets/
Paper trading: use paper API keys; ``paper_mode=True`` → ``TradingClient(..., paper=True)``.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import ROUND_DOWN, ROUND_UP, Decimal
from typing import Any, AsyncIterator, Callable, TypeVar

from alpaca.common.exceptions import APIError
from alpaca.data.enums import CryptoFeed
from alpaca.data.historical.crypto import CryptoHistoricalDataClient
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.requests import (
    CryptoBarsRequest,
    CryptoLatestOrderbookRequest,
    CryptoLatestTradeRequest,
    StockBarsRequest,
    StockLatestTradeRequest,
    StockSnapshotRequest,
)
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import (
    AssetClass as AlpAssetClass,
    AssetStatus,
    OrderSide as AlpOrderSide,
    OrderStatus as AlpOrdStatus,
    PositionSide,
    QueryOrderStatus,
    TimeInForce as AlpTIF,
)
from alpaca.trading.models import Order as AlpOrder
from alpaca.trading.models import Position as AlpPosition
from alpaca.trading.requests import (
    GetAssetsRequest,
    GetOrdersRequest,
    LimitOrderRequest,
    MarketOrderRequest,
    StopLimitOrderRequest,
    StopOrderRequest,
)
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

T = TypeVar("T")

_TIMEFRAME_MINUTES: dict[str, int] = {
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


def _dt_to_iso_z(dt: datetime | None) -> str:
    if dt is None:
        return _iso_now()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _aware_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# Delayed / bundled feeds often return a very old L1 while latest trade is current — ignore those.
_STALE_QUOTE_MAX_AGE_SEC = 7200
# Liquid US equities rarely show >~1% NBBO width; wider usually means bad or synthetic data.
_MAX_QUOTE_REL_SPREAD = Decimal("0.012")


def _alpaca_equity_l1_usable(
    bid: float,
    ask: float,
    quote_ts: datetime | None,
    trade_ts: datetime | None,
) -> bool:
    if bid <= 0 or ask <= 0 or bid >= ask:
        return False
    bid_d, ask_d = Decimal(str(bid)), Decimal(str(ask))
    mid = (bid_d + ask_d) / Decimal(2)
    if mid <= 0:
        return False
    rel_spread = (ask_d - bid_d) / mid
    if rel_spread > _MAX_QUOTE_REL_SPREAD:
        return False
    q = _aware_utc(quote_ts)
    if q is None:
        return False
    if (datetime.now(timezone.utc) - q).total_seconds() > _STALE_QUOTE_MAX_AGE_SEC:
        return False
    t = _aware_utc(trade_ts)
    if t is not None and t > q and (t - q).total_seconds() > 300:
        return False
    return True


def _resolve_snap_symbol(snaps: dict[str, Any], sym: str) -> Any:
    s = snaps.get(sym)
    if s is not None:
        return s
    u = sym.upper()
    for k, v in snaps.items():
        if k.upper() == u:
            return v
    return None


def _stock_snapshot_l1(
    stock: StockHistoricalDataClient,
    sym: str,
) -> tuple[Decimal, list[tuple[Decimal, Decimal]], list[tuple[Decimal, Decimal]], str]:
    """
    One snapshot fetch: last trade price + top-of-book bids/asks.
    Falls back to bid=ask=last when L1 is stale or has an absurd spread (common on free/delayed feeds).
    When that fallback is used, the returned timestamp is wall-clock (poll time) so logs match reality.
    """
    req = StockSnapshotRequest(symbol_or_symbols=sym)
    snaps = stock.get_stock_snapshot(req)
    snap = _resolve_snap_symbol(snaps, sym)
    if snap is None:
        return Decimal(0), [], [], _iso_now()
    lt = snap.latest_trade
    lq = snap.latest_quote
    last = _d(lt.price) if lt else Decimal(0)
    trade_ts = lt.timestamp if lt else None
    quote_ts = lq.timestamp if lq else None

    use_l1 = False
    if lq is not None:
        use_l1 = _alpaca_equity_l1_usable(
            float(lq.bid_price),
            float(lq.ask_price),
            quote_ts,
            trade_ts,
        )
    if use_l1 and lq is not None:
        bp, ap = _d(lq.bid_price), _d(lq.ask_price)
        bsz, asz = _d(lq.bid_size), _d(lq.ask_size)
        if last <= 0 and bp > 0 and ap > 0:
            last = (bp + ap) / Decimal(2)
        return last, [(bp, bsz)], [(ap, asz)], _dt_to_iso_z(quote_ts or trade_ts)
    if last > 0:
        return last, [(last, Decimal(0))], [(last, Decimal(0))], _iso_now()
    return Decimal(0), [], [], _iso_now()


def _is_crypto_symbol(symbol: str) -> bool:
    return "/" in symbol.strip()


def _format_alpaca_error(exc: BaseException) -> str:
    """Extract a concise human-readable reason from an Alpaca SDK error."""
    import json as _json

    msg = str(exc).strip()
    # Alpaca wraps its JSON payload in the exception message — peel it out.
    brace = msg.find("{")
    if brace >= 0:
        tail = msg[brace:]
        try:
            data = _json.loads(tail)
            if isinstance(data, dict):
                reason = data.get("message") or data.get("error") or ""
                if isinstance(reason, str) and reason.strip():
                    code = data.get("code")
                    return f"{reason.strip()}" + (f" [code={code}]" if code else "")
        except Exception:  # noqa: BLE001
            pass
    # Fallback: collapse HTTPError boilerplate.
    if " for url:" in msg:
        msg = msg.split(" for url:", 1)[0]
    return msg[:240]


# ─── Alpaca US-equity tick size rules ──────────────────────────────────────
# Per NMS Rule 612: stocks priced >= $1.00 quote in $0.01 increments; stocks
# priced < $1.00 may quote in $0.0001 increments. Alpaca enforces this server-
# side and returns a 422 "sub-penny increment" error if we submit anything
# finer. Any model output like 28.53499985 must be rounded before submission.
_ALPACA_EQUITY_PENNY = Decimal("0.01")
_ALPACA_EQUITY_SUBPENNY = Decimal("0.0001")
_ALPACA_EQUITY_SUBPENNY_CUTOFF = Decimal("1.00")
# Alpaca crypto allows up to 8 decimals — keep model precision but still clamp
# to something sane so we never submit 15+ digit floats.
_ALPACA_CRYPTO_TICK = Decimal("0.00000001")


def _alpaca_price_tick(symbol: str, price: Decimal) -> Decimal:
    """Return the minimum price increment Alpaca will accept for ``symbol``."""
    if _is_crypto_symbol(symbol):
        return _ALPACA_CRYPTO_TICK
    if price < _ALPACA_EQUITY_SUBPENNY_CUTOFF:
        return _ALPACA_EQUITY_SUBPENNY
    return _ALPACA_EQUITY_PENNY


def _round_price_to_tick(
    symbol: str,
    price: Decimal,
    *,
    side: AlpOrderSide,
    is_stop: bool = False,
) -> Decimal:
    """Quantize a price to the nearest valid Alpaca tick, biased defensively.

    We intentionally do **not** round to the mathematically-nearest tick because
    that can flip the order across a penny and make fills slightly more
    aggressive than the model intended. Instead we bias each direction so the
    resulting order is always at least as passive as the raw model price:

    * ``LIMIT BUY``  → round DOWN (won't pay more than model requested)
    * ``LIMIT SELL`` → round UP   (won't sell cheaper than model requested)
    * ``STOP BUY``   → round UP   (breakout trigger needs more momentum)
    * ``STOP SELL``  → round DOWN (protective stop gives position more room)
    """
    if price <= 0:
        return price
    tick = _alpaca_price_tick(symbol, price)
    if side == AlpOrderSide.BUY:
        rounding = ROUND_UP if is_stop else ROUND_DOWN
    else:
        rounding = ROUND_DOWN if is_stop else ROUND_UP
    q = (price / tick).quantize(Decimal("1"), rounding=rounding) * tick
    # Re-evaluate tick in case the rounded price crossed the $1 boundary.
    new_tick = _alpaca_price_tick(symbol, q)
    if new_tick != tick:
        q = (price / new_tick).quantize(Decimal("1"), rounding=rounding) * new_tick
    return q.quantize(new_tick)


def _bars_timeframe(timeframe: str) -> TimeFrame:
    m: dict[str, TimeFrame] = {
        "1m": TimeFrame(1, TimeFrameUnit.Minute),
        "5m": TimeFrame(5, TimeFrameUnit.Minute),
        "15m": TimeFrame(15, TimeFrameUnit.Minute),
        "30m": TimeFrame(30, TimeFrameUnit.Minute),
        "1h": TimeFrame(1, TimeFrameUnit.Hour),
        "4h": TimeFrame(4, TimeFrameUnit.Hour),
        "1d": TimeFrame(1, TimeFrameUnit.Day),
    }
    if timeframe not in m:
        raise ValueError(f"Unsupported timeframe: {timeframe}")
    return m[timeframe]


def _estimate_bars_start_utc(timeframe: str, limit: int) -> datetime:
    minutes_per = _TIMEFRAME_MINUTES.get(timeframe, 60)
    span = int(minutes_per * max(limit, 1) * 1.25 + 120)
    return (datetime.now(timezone.utc) - timedelta(minutes=span)).replace(tzinfo=None)


def _map_tif(tif: str | None) -> AlpTIF:
    u = (tif or "GTC").strip().upper()
    if u in ("DAY", "DAY_ORDER"):
        return AlpTIF.DAY
    if u in ("IOC",):
        return AlpTIF.IOC
    if u in ("FOK",):
        return AlpTIF.FOK
    if u in ("OPG", "MOO", "LOO"):
        return AlpTIF.OPG
    if u in ("CLS", "MOC", "LOC"):
        return AlpTIF.CLS
    return AlpTIF.GTC


def _alp_ord_status_to_base(s: AlpOrdStatus) -> OrderStatus:
    if s in (
        AlpOrdStatus.NEW,
        AlpOrdStatus.ACCEPTED,
        AlpOrdStatus.ACCEPTED_FOR_BIDDING,
        AlpOrdStatus.PENDING_NEW,
        AlpOrdStatus.DONE_FOR_DAY,
        AlpOrdStatus.CALCULATED,
        AlpOrdStatus.HELD,
    ):
        return OrderStatus.OPEN
    if s == AlpOrdStatus.PARTIALLY_FILLED:
        return OrderStatus.PARTIALLY_FILLED
    if s == AlpOrdStatus.FILLED:
        return OrderStatus.FILLED
    if s in (
        AlpOrdStatus.CANCELED,
        AlpOrdStatus.EXPIRED,
        AlpOrdStatus.REPLACED,
        AlpOrdStatus.STOPPED,
    ):
        return OrderStatus.CANCELLED
    if s == AlpOrdStatus.REJECTED:
        return OrderStatus.REJECTED
    if s in (
        AlpOrdStatus.PENDING_CANCEL,
        AlpOrdStatus.PENDING_REPLACE,
        AlpOrdStatus.PENDING_REVIEW,
        AlpOrdStatus.SUSPENDED,
    ):
        return OrderStatus.PENDING
    return OrderStatus.PENDING


def _alp_order_to_result(o: AlpOrder) -> OrderResult:
    side = OrderSide.BUY if o.side == AlpOrderSide.BUY else OrderSide.SELL
    if o.qty is not None:
        qty = _d(o.qty)
    elif o.notional is not None:
        qty = _d(o.notional)
    else:
        qty = Decimal(0)
    filled = _d(o.filled_qty or 0)
    avg: Decimal | None = None
    if o.filled_avg_price is not None:
        ap = _d(o.filled_avg_price)
        if ap > 0:
            avg = ap
    return OrderResult(
        broker_order_id=str(o.id),
        client_order_id=o.client_order_id or None,
        status=_alp_ord_status_to_base(o.status),
        symbol=o.symbol,
        side=side,
        quantity=qty,
        filled_quantity=filled,
        avg_fill_price=avg,
        fee=None,
        timestamp=_dt_to_iso_z(o.updated_at),
    )


def _alp_asset_class_to_base(ac: AlpAssetClass) -> AssetClass:
    if ac == AlpAssetClass.CRYPTO:
        return AssetClass.CRYPTO
    if ac == AlpAssetClass.US_OPTION:
        return AssetClass.OPTION
    if ac == AlpAssetClass.US_EQUITY:
        return AssetClass.EQUITY
    return AssetClass.EQUITY


def _api_error_is_not_found(exc: APIError) -> bool:
    return getattr(exc, "status_code", None) == 404


class AlpacaAdapter(BrokerAdapter):
    """
    Alpaca REST adapter. ``paper_mode=True`` uses the paper trading API (paper keys).
    Market data uses the same key pair; crypto bars/trades may work without keys at reduced limits.
    """

    broker_name = "alpaca"

    PAPER_URL = "https://paper-api.alpaca.markets"
    LIVE_URL = "https://api.alpaca.markets"

    def __init__(
        self,
        api_key: str = "",
        api_secret: str = "",
        paper_mode: bool = True,
        **kwargs: object,
    ) -> None:
        _ = kwargs
        self.api_key = api_key or ""
        self.api_secret = api_secret or ""
        self.paper_mode = paper_mode
        self.base_url = self.PAPER_URL if paper_mode else self.LIVE_URL
        self._lock = asyncio.Lock()
        self._connected = False
        self._trading: TradingClient | None = None
        self._stock: StockHistoricalDataClient | None = None
        self._crypto: CryptoHistoricalDataClient | None = None

    async def _run_sync(self, fn: Callable[[], T]) -> T:
        async with self._lock:
            return await asyncio.to_thread(fn)

    def _require_trading(self) -> TradingClient:
        if self._trading is None:
            raise RuntimeError("Alpaca trading client not available (connect with API keys)")
        return self._trading

    def _build_order_request(self, order: Order) -> MarketOrderRequest | LimitOrderRequest | StopOrderRequest | StopLimitOrderRequest:
        sym = order.symbol.strip()
        side = AlpOrderSide.BUY if order.side == OrderSide.BUY else AlpOrderSide.SELL
        tif = _map_tif(order.time_in_force)
        qty = float(order.quantity)
        cid = order.client_order_id

        common: dict = {"symbol": sym, "side": side, "time_in_force": tif, "qty": qty}
        if cid:
            common["client_order_id"] = cid

        if order.order_type == OrderType.MARKET:
            return MarketOrderRequest(**common)
        if order.order_type == OrderType.LIMIT:
            if order.limit_price is None:
                raise ValueError("limit_price required for LIMIT orders")
            limit_px = _round_price_to_tick(sym, _d(order.limit_price), side=side, is_stop=False)
            return LimitOrderRequest(**common, limit_price=float(limit_px))
        if order.order_type == OrderType.STOP:
            if order.stop_price is None:
                raise ValueError("stop_price required for STOP orders")
            stop_px = _round_price_to_tick(sym, _d(order.stop_price), side=side, is_stop=True)
            return StopOrderRequest(**common, stop_price=float(stop_px))
        if order.order_type == OrderType.STOP_LIMIT:
            if order.limit_price is None or order.stop_price is None:
                raise ValueError("limit_price and stop_price required for STOP_LIMIT orders")
            limit_px = _round_price_to_tick(sym, _d(order.limit_price), side=side, is_stop=False)
            stop_px = _round_price_to_tick(sym, _d(order.stop_price), side=side, is_stop=True)
            return StopLimitOrderRequest(
                **common,
                stop_price=float(stop_px),
                limit_price=float(limit_px),
            )
        raise ValueError(f"Unsupported order type: {order.order_type}")

    def _alp_position_to_base(self, p: AlpPosition) -> Position:
        q = _d(p.qty)
        if p.side == PositionSide.SHORT:
            q = -abs(q)
        else:
            q = abs(q)
        px = p.current_price or (p.usd.current_price if p.usd else None)
        cur = _d(px or "0")
        u_pnl = p.unrealized_pl or (p.usd.unrealized_pl if p.usd else None)
        return Position(
            symbol=p.symbol,
            asset_class=_alp_asset_class_to_base(p.asset_class),
            quantity=q,
            avg_entry_price=_d(p.avg_entry_price),
            current_price=cur,
            unrealised_pnl=_d(u_pnl or "0"),
            broker=self.broker_name,
        )

    async def connect(self) -> bool:
        if not self.api_key or not self.api_secret:
            logger.error("connect | Alpaca | missing ALPACA_API_KEY or ALPACA_API_SECRET")
            return False
        try:
            self._trading = TradingClient(
                api_key=self.api_key,
                secret_key=self.api_secret,
                paper=self.paper_mode,
            )
            self._stock = StockHistoricalDataClient(
                api_key=self.api_key,
                secret_key=self.api_secret,
            )
            self._crypto = CryptoHistoricalDataClient(
                api_key=self.api_key,
                secret_key=self.api_secret,
            )

            def _ping() -> None:
                assert self._trading is not None
                self._trading.get_account()

            await self._run_sync(_ping)
            self._connected = True
            logger.info(
                "connect | Alpaca | paper_mode={} base_url={}",
                self.paper_mode,
                self.base_url,
            )
            return True
        except Exception as exc:  # noqa: BLE001
            logger.exception("connect | Alpaca | failed | error={}", exc)
            self._trading = None
            self._stock = None
            self._crypto = None
            self._connected = False
            return False

    async def disconnect(self) -> None:
        self._trading = None
        self._stock = None
        self._crypto = None
        self._connected = False

    async def is_connected(self) -> bool:
        return self._connected and self._trading is not None

    async def get_balance(self) -> list[Balance]:
        tc = self._require_trading()

        def _go() -> list[Balance]:
            acc = tc.get_account()
            cur = (acc.currency or "USD").upper()
            cash = _d(acc.cash or "0")
            equity = _d(acc.equity or acc.portfolio_value or "0")
            # Align with IBKR-style semantics: total ≈ net Liq, available ≈ spendable cash.
            # Buying power can exceed equity on margin; it is not stored on Balance.
            available = cash
            reserved = equity - available
            if reserved < 0:
                reserved = Decimal(0)
            logger.debug(
                "get_balance | Alpaca | equity={} cash={} buying_power={}",
                equity,
                cash,
                _d(acc.buying_power or "0"),
            )
            return [
                Balance(
                    currency=cur,
                    total=equity,
                    available=available,
                    reserved=reserved,
                )
            ]

        return await self._run_sync(_go)

    async def get_positions(self) -> list[Position]:
        tc = self._require_trading()

        def _go() -> list[Position]:
            raw = tc.get_all_positions()
            return [self._alp_position_to_base(p) for p in raw]

        return await self._run_sync(_go)

    async def place_order(self, order: Order) -> OrderResult:
        tc = self._require_trading()
        req = self._build_order_request(order)

        def _existing_by_client_id() -> AlpOrder | None:
            if not order.client_order_id:
                return None
            try:
                return tc.get_order_by_client_id(order.client_order_id)
            except APIError as exc:
                if _api_error_is_not_found(exc):
                    return None
                raise

        def _submit() -> AlpOrder:
            ex = _existing_by_client_id()
            if ex is not None:
                return ex
            return tc.submit_order(req)

        try:
            alp_o = await self._run_sync(_submit)
            return _alp_order_to_result(alp_o)
        except Exception as exc:  # noqa: BLE001
            err = _format_alpaca_error(exc)
            logger.warning(
                "place_order | Alpaca | rejected | symbol={} | side={} | error={}",
                order.symbol.strip(),
                order.side.value,
                err,
            )
            # Stash the broker's reason on the order so ``persist_order_log``
            # captures it into ``OrderLog.instrument_metadata`` and the Risk UI
            # can display *why* the broker refused this order.
            meta = dict(order.instrument_metadata or {})
            meta.setdefault("error_message", err)
            meta.setdefault("rejected_by", "alpaca")
            order.instrument_metadata = meta
            return OrderResult(
                broker_order_id="",
                client_order_id=order.client_order_id,
                status=OrderStatus.REJECTED,
                symbol=order.symbol.strip(),
                side=order.side,
                quantity=order.quantity,
                filled_quantity=Decimal(0),
                avg_fill_price=None,
                fee=None,
                timestamp=_iso_now(),
            )

    async def cancel_order(self, broker_order_id: str) -> bool:
        tc = self._require_trading()

        def _go() -> bool:
            try:
                tc.cancel_order_by_id(broker_order_id)
                return True
            except APIError as exc:
                logger.warning("cancel_order | Alpaca | id={} | error={}", broker_order_id, exc)
                return False

        return await self._run_sync(_go)

    async def get_order(self, broker_order_id: str) -> OrderResult:
        tc = self._require_trading()

        def _go() -> OrderResult:
            o = tc.get_order_by_id(broker_order_id)
            return _alp_order_to_result(o)

        try:
            return await self._run_sync(_go)
        except Exception as exc:  # noqa: BLE001
            logger.warning("get_order | Alpaca | id={} | error={}", broker_order_id, exc)
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
        tc = self._require_trading()

        def _go() -> list[OrderResult]:
            filt = GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=500)
            orders = tc.get_orders(filter=filt)
            return [_alp_order_to_result(o) for o in orders]

        return await self._run_sync(_go)

    async def get_candles(self, symbol: str, timeframe: str, limit: int = 200) -> list[Candle]:
        if not self._connected:
            return []
        tf = _bars_timeframe(timeframe)
        start = _estimate_bars_start_utc(timeframe, limit)
        sym = symbol.strip()

        if _is_crypto_symbol(sym):
            if self._crypto is None:
                return []

            def _crypto_go() -> list[Candle]:
                req = CryptoBarsRequest(
                    symbol_or_symbols=sym,
                    timeframe=tf,
                    start=start,
                    limit=min(limit, 10000),
                )
                barset = self._crypto.get_crypto_bars(req, feed=CryptoFeed.US)
                rows = barset.data.get(sym, []) if hasattr(barset, "data") else []
                out: list[Candle] = []
                for bar in rows[-limit:]:
                    out.append(
                        Candle(
                            symbol=sym,
                            timestamp=_dt_to_iso_z(bar.timestamp),
                            open=_d(bar.open),
                            high=_d(bar.high),
                            low=_d(bar.low),
                            close=_d(bar.close),
                            volume=_d(bar.volume),
                            timeframe=timeframe,
                        )
                    )
                return out

            return await self._run_sync(_crypto_go)

        if self._stock is None:
            return []

        def _stock_go() -> list[Candle]:
            req = StockBarsRequest(
                symbol_or_symbols=sym,
                timeframe=tf,
                start=start,
                limit=min(limit, 10000),
            )
            barset = self._stock.get_stock_bars(req)
            rows = barset.data.get(sym, [])
            if not rows:
                for k, v in barset.data.items():
                    if k.upper() == sym.upper():
                        rows = v
                        break
            out: list[Candle] = []
            for bar in rows[-limit:]:
                out.append(
                    Candle(
                        symbol=sym,
                        timestamp=_dt_to_iso_z(bar.timestamp),
                        open=_d(bar.open),
                        high=_d(bar.high),
                        low=_d(bar.low),
                        close=_d(bar.close),
                        volume=_d(bar.volume),
                        timeframe=timeframe,
                    )
                )
            return out

        return await self._run_sync(_stock_go)

    async def get_order_book(self, symbol: str, depth: int = 10) -> OrderBook:
        sym = symbol.strip()
        d = max(1, min(depth, 50))

        if _is_crypto_symbol(sym):
            if self._crypto is None:
                raise RuntimeError("not connected")

            def _crypto_book() -> OrderBook:
                req = CryptoLatestOrderbookRequest(symbol_or_symbols=sym)
                books = self._crypto.get_crypto_latest_orderbook(req, feed=CryptoFeed.US)
                ob = books.get(sym) or next(iter(books.values()), None)
                if ob is None:
                    return OrderBook(symbol=sym, timestamp=_iso_now(), bids=[], asks=[])
                bids = [(Decimal(str(x.price)), Decimal(str(x.size))) for x in ob.bids[:d]]
                asks = [(Decimal(str(x.price)), Decimal(str(x.size))) for x in ob.asks[:d]]
                return OrderBook(
                    symbol=sym,
                    timestamp=_dt_to_iso_z(ob.timestamp),
                    bids=bids,
                    asks=asks,
                )

            return await self._run_sync(_crypto_book)

        if self._stock is None:
            raise RuntimeError("not connected")

        def _stock_snap_book() -> OrderBook:
            assert self._stock is not None
            last, bids, asks, ts = _stock_snapshot_l1(self._stock, sym)
            _ = last
            return OrderBook(symbol=sym, timestamp=ts, bids=bids[:d], asks=asks[:d])

        return await self._run_sync(_stock_snap_book)

    async def get_last_price(self, symbol: str) -> Decimal:
        sym = symbol.strip()
        if _is_crypto_symbol(sym):
            if self._crypto is None:
                return Decimal(0)

            def _ct() -> Decimal:
                req = CryptoLatestTradeRequest(symbol_or_symbols=sym)
                trades = self._crypto.get_crypto_latest_trade(req, feed=CryptoFeed.US)
                t = trades.get(sym) or next(iter(trades.values()), None)
                if t is None:
                    return Decimal(0)
                return _d(t.price)

            return await self._run_sync(_ct)

        if self._stock is None:
            return Decimal(0)

        def _st() -> Decimal:
            req = StockLatestTradeRequest(symbol_or_symbols=sym)
            trades = self._stock.get_stock_latest_trade(req)
            tr = trades.get(sym) or next(iter(trades.values()), None)
            if tr is None:
                return Decimal(0)
            return _d(tr.price)

        return await self._run_sync(_st)

    async def stream_prices(self, symbols: list[str]) -> AsyncIterator[Tick]:
        if not symbols:
            return
        while self._connected:
            for sym in symbols:
                if not self._connected:
                    break
                s = sym.strip()
                try:
                    if _is_crypto_symbol(s):
                        last = await self.get_last_price(s)
                        ob = await self.get_order_book(s, depth=5)
                        bid = ob.bids[0][0] if ob.bids else None
                        ask = ob.asks[0][0] if ob.asks else None
                        ts = ob.timestamp if ob.bids or ob.asks else _iso_now()
                    else:
                        if self._stock is None:
                            continue

                        def _eq_tick() -> Tick:
                            assert self._stock is not None
                            px, bids, asks, ts = _stock_snapshot_l1(self._stock, s)
                            bid = bids[0][0] if bids else None
                            ask = asks[0][0] if asks else None
                            return Tick(
                                symbol=s,
                                timestamp=ts,
                                price=px,
                                volume=Decimal(0),
                                bid=bid,
                                ask=ask,
                            )

                        tick = await self._run_sync(_eq_tick)
                        yield tick
                        continue
                    yield Tick(
                        symbol=s,
                        timestamp=ts,
                        price=last,
                        volume=Decimal(0),
                        bid=bid,
                        ask=ask,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("stream_prices | Alpaca | symbol={} | error={}", s, exc)
            await asyncio.sleep(1.0)

    async def get_supported_symbols(self) -> list[str]:
        tc = self._require_trading()

        def _go() -> list[str]:
            filt = GetAssetsRequest(status=AssetStatus.ACTIVE)
            assets = tc.get_all_assets(filter=filt)
            return sorted({a.symbol for a in assets if a.tradable})

        return await self._run_sync(_go)

    async def get_asset_class(self, symbol: str) -> AssetClass:
        tc = self._require_trading()

        def _go() -> AssetClass:
            a = tc.get_asset(symbol.strip())
            return _alp_asset_class_to_base(a.asset_class)

        return await self._run_sync(_go)
