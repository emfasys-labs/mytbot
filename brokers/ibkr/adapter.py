"""
brokers/ibkr/adapter.py
========================
Interactive Brokers adapter.
Primary broker — stocks, bonds, ETFs, forex, options, futures, crypto (11 coins).

SDK: pip install ib_insync
Docs: https://ib-insync.readthedocs.io/
TWS API: https://interactivebrokers.github.io/tws-api/

Setup:
- Install TWS or IB Gateway on your machine
- Enable API connections in TWS: Edit → Global Config → API → Settings
- Paper trading port: 7497
- Live trading port:  7496
"""

from __future__ import annotations

import asyncio
import os
from collections import defaultdict
from datetime import date, datetime, timezone
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from typing import Any, AsyncIterator, Optional, Set

from ib_insync import (
    IB,
    Contract,
    Crypto,
    Forex,
    LimitOrder,
    MarketOrder,
    Option,
    Stock,
    StopLimitOrder,
    StopOrder,
    Ticker,
    Trade,
    util,
)
from loguru import logger

from core.instruments import OptionContractSpec
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
from system.live_arming import validate_ibkr_port_arming

# ib_insync expects asyncio integration when using connectAsync outside its own loop.
util.patchAsyncio()

# Paxos crypto on IBKR: bare symbol like "BTC" implies USD quote.
#
# Paper trading caveat: PAXOS-routed crypto orders often stay Inactive or do not
# complete a realistic open→fill→close cycle on IBKR paper accounts. For an
# end-to-end broker smoke (Telegram + open/hold/close), prefer a liquid
# equity/FX symbol (e.g. ``SPY`` where your jurisdiction allows it, or
# ``EURUSD``); see repo root ``test_telegram_trade_cycle.py`` docstring.
_KNOWN_PAXOS_CRYPTO: frozenset[str] = frozenset(
    {
        "BTC",
        "ETH",
        "LTC",
        "BCH",
        "PAXG",
        "SOL",
        "ADA",
        "DOGE",
        "LINK",
        "MATIC",
        "DOT",
    }
)


def _total_from_account_summary_tags(tags: dict[str, Decimal]) -> Decimal:
    """Pick one equity figure per currency from IB account-summary tag bag.

    **NetLiquidation** is full account NAV (incl. margin positions). For USD-only accounts IB
    often emits only ``currency=USD`` rows (no BASE line in ``get_balance`` output); preferring
    cash tags first would understate equity — see :func:`system.portfolio_equity.live_portfolio_value`.
    """
    return (
        tags.get("NetLiquidation")
        or tags.get("TotalCashValue")
        or tags.get("CashBalance")
        or Decimal(0)
    )


def _d(v: object) -> Decimal:
    """Convert a numeric API value to Decimal (never float in public models)."""
    if v is None:
        return Decimal(0)
    if isinstance(v, Decimal):
        return v
    if isinstance(v, (int,)):
        return Decimal(int(v))
    if isinstance(v, float) and v != v:  # NaN
        return Decimal(0)
    return Decimal(str(v))


def _is_bad_price(v: object) -> bool:
    """True if *v* is missing or non-finite (e.g. IBKR NaN placeholders)."""
    if v is None:
        return True
    if isinstance(v, float) and (v != v or v in (float("inf"), float("-inf"))):
        return True
    return False


def _iso_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _contract_expiry_yyyymmdd(contract: Contract) -> str:
    exp = getattr(contract, "lastTradeDateOrContractMonth", None)
    if exp is None:
        return ""
    if hasattr(exp, "strftime"):
        return exp.strftime("%Y%m%d")
    s = str(exp).strip().replace("-", "")
    return s[:8] if len(s) >= 8 else s


def _option_contract_position_key(contract: Contract) -> str:
    und = (contract.symbol or "").strip().upper()
    e = _contract_expiry_yyyymmdd(contract)
    r = (getattr(contract, "right", None) or "").strip().upper()[:1] or "?"
    stk = _d(getattr(contract, "strike", 0) or 0)
    return f"{und}|{e}|{r}|{stk}"


class IBKRAdapter(BrokerAdapter):
    """
    Adapter for Interactive Brokers via ib_insync.
    Supports: US/UK/EU equities, ETFs, bonds, forex, options, futures, crypto.
    """

    broker_name = "ibkr"

    _BAR_SIZE: dict[str, str] = {
        "1m": "1 min",
        "5m": "5 mins",
        "15m": "15 mins",
        "30m": "30 mins",
        "1h": "1 hour",
        "4h": "4 hours",
        "1d": "1 day",
    }

    _TF_MINUTES: dict[str, int] = {
        "1m": 1,
        "5m": 5,
        "15m": 15,
        "30m": 30,
        "1h": 60,
        "4h": 240,
        "1d": 24 * 60,
    }

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 7497,  # 7497 = paper, 7496 = live
        client_id: int = 1,
        account_id: str = "",
        paper_mode: bool = True,
        **kwargs: Any,
    ) -> None:
        validate_ibkr_port_arming(paper_mode=paper_mode, port=7496 if not paper_mode else port)
        self.host = host
        self.port = port if paper_mode else 7496
        self.client_id = client_id
        self.account_id = account_id
        self.paper_mode = paper_mode
        # BrokerManager reads this when connect() returns False so UI shows a useful reason.
        self._last_connect_error: str | None = None
        self._ib: Optional[IB] = None
        self._last_ib_order_snapshot_monotonic: float = -1e9
        self._ib_order_snap_lock = asyncio.Lock()

    def _resolve_account(self) -> str:
        """Return configured account or the sole managed account from IB."""
        if self.account_id:
            return self.account_id
        if self._ib is None:
            return ""
        accounts = self._ib.managedAccounts()
        if len(accounts) == 1:
            return accounts[0]
        return accounts[0] if accounts else ""

    def _symbol_to_contract(self, symbol: str) -> Contract:
        """Map a canonical symbol string to an ib_insync Contract."""
        s = symbol.strip().upper()
        if "/" in s:
            base, quote = s.split("/", 1)
            quote = quote.strip().upper()
            return Crypto(base.strip(), "PAXOS", quote)
        if s in _KNOWN_PAXOS_CRYPTO:
            return Crypto(s, "PAXOS", "USD")
        if len(s) == 6 and s.isalpha():
            return Forex(s[:3] + s[3:])
        return Stock(s, "SMART", "USD")

    def build_option_contract(self, spec: OptionContractSpec) -> Option:
        """Build an unqualified ib_insync Option from a structured spec."""
        exp = spec.expiry_yyyymmdd()
        mult = str(int(spec.multiplier)) if spec.multiplier else "100"
        return Option(
            spec.underlying_symbol.strip().upper(),
            exp,
            float(str(spec.strike)),
            spec.right.value,
            spec.exchange or "SMART",
            currency=spec.currency or "USD",
            multiplier=mult,
        )

    def _order_to_contract(self, order: Order) -> Contract:
        meta = getattr(order, "instrument_metadata", None)
        if isinstance(meta, dict) and meta.get("instrument_type") == "option":
            raw = meta.get("option_contract")
            if isinstance(raw, dict):
                spec = OptionContractSpec.from_dict(raw)
                return self.build_option_contract(spec)
        return self._symbol_to_contract(order.symbol)

    def _contract_symbol_key(self, contract: Contract) -> str:
        """Produce a display symbol for ticks/positions."""
        st = (contract.secType or "").upper()
        if st == "OPT":
            ls = (getattr(contract, "localSymbol", None) or "").strip()
            if ls:
                return ls
            return _option_contract_position_key(contract)
        if st == "CRYPTO":
            return f"{contract.symbol}/{contract.currency}"
        if st == "CASH":
            cur = contract.symbol or ""
            if len(cur) >= 6:
                return f"{cur[:3]}/{cur[3:]}"
            return cur
        return contract.symbol or ""

    def _option_metadata_from_contract(self, contract: Contract) -> dict[str, Any]:
        mult_raw = getattr(contract, "multiplier", None) or 100
        try:
            mult_i = int(float(mult_raw))
        except Exception:  # noqa: BLE001
            mult_i = 100
        right_c = (getattr(contract, "right", None) or "").strip().upper()[:1] or "C"
        try:
            strike_d = _d(getattr(contract, "strike", 0) or 0)
        except Exception:  # noqa: BLE001
            strike_d = Decimal(0)
        return {
            "instrument_type": "option",
            "underlying_symbol": (contract.symbol or "").strip().upper(),
            "expiry": _contract_expiry_yyyymmdd(contract),
            "strike": str(strike_d),
            "right": right_c,
            "multiplier": mult_i,
            "exchange": (contract.exchange or "SMART").strip() or "SMART",
            "currency": (contract.currency or "USD").strip() or "USD",
            "sec_type": "OPT",
            "local_symbol": (getattr(contract, "localSymbol", None) or "").strip() or None,
        }

    def _asset_class_from_contract(self, contract: Contract) -> AssetClass:
        st = (contract.secType or "").upper()
        if st == "STK":
            return AssetClass.EQUITY
        if st == "CRYPTO":
            return AssetClass.CRYPTO
        if st == "CASH":
            return AssetClass.FOREX
        if st == "OPT":
            return AssetClass.OPTION
        if st in ("FUT", "CONTFUT"):
            return AssetClass.FUTURE
        if st == "BOND":
            return AssetClass.BOND
        return AssetClass.EQUITY

    def _duration_str(self, timeframe: str, limit: int) -> str:
        """Pick a durationStr that likely covers ``limit`` bars."""
        minutes_per = self._TF_MINUTES.get(timeframe, 1)
        total_min = max(1, minutes_per * limit * 2)
        days = max(1, min(365, int(total_min / (60 * 24)) + 2))
        return f"{days} D"

    def _trade_broker_id(self, trade: Trade) -> str:
        pid = trade.order.permId
        if pid and pid > 0:
            return str(pid)
        return str(trade.order.orderId)

    def _filled_from_executions(self, trade: Trade) -> Decimal:
        """Sum execution shares when orderStatus lags behind execDetails (common after slow sync)."""
        total = Decimal(0)
        for f in trade.fills:
            sh = getattr(f.execution, "shares", None)
            if sh is not None:
                total += _d(sh)
        return total

    def _effective_filled_qty(self, trade: Trade) -> Decimal:
        os_filled = _d(trade.orderStatus.filled)
        return max(os_filled, self._filled_from_executions(trade))

    @staticmethod
    def _remaining_safe(trade: Trade) -> float:
        """IBKR ``remaining`` can be NaN or unset for PAXOS cash-qty orders; treat as 0 when invalid."""
        r = trade.orderStatus.remaining
        if r is None:
            return 0.0
        try:
            v = float(r)
        except (TypeError, ValueError):
            return 0.0
        if v != v:  # NaN
            return 0.0
        return v

    def _map_ib_status(self, trade: Trade) -> OrderStatus:
        s = trade.orderStatus.status or ""
        if s in ("Cancelled", "ApiCancelled"):
            return OrderStatus.CANCELLED
        if s == "Filled":
            return OrderStatus.FILLED
        if s == "Inactive":
            return OrderStatus.REJECTED
        if s == "PendingCancel":
            return OrderStatus.PENDING
        qty = _d(trade.order.totalQuantity)
        filled_e = self._effective_filled_qty(trade)
        rem = self._remaining_safe(trade)
        if qty > 0 and filled_e >= qty:
            return OrderStatus.FILLED
        # PAXOS crypto: IB uses totalQuantity=0 + cashQty; fills still land on orderStatus / executions
        if qty == 0 and filled_e > 0 and rem <= 0:
            return OrderStatus.FILLED
        if filled_e > 0 and rem > 0:
            return OrderStatus.PARTIALLY_FILLED
        if s in ("PendingSubmit", "ApiPending", "PreSubmitted"):
            return OrderStatus.PENDING
        if s == "Submitted":
            return OrderStatus.OPEN
        return OrderStatus.PENDING

    def _trade_fee(self, trade: Trade) -> Optional[Decimal]:
        total = Decimal(0)
        seen = False
        for f in trade.fills:
            cr = f.commissionReport
            if cr is None or cr.commission is None:
                continue
            seen = True
            total += _d(cr.commission)
        return total if seen else None

    def _trade_to_order_result(self, trade: Trade) -> OrderResult:
        sym = self._contract_symbol_key(trade.contract)
        side = OrderSide.BUY if trade.order.action == "BUY" else OrderSide.SELL
        qty = _d(trade.order.totalQuantity)
        filled = self._effective_filled_qty(trade)
        avg = trade.orderStatus.avgFillPrice
        avg_d = _d(avg) if avg and avg > 0 else None
        return OrderResult(
            broker_order_id=self._trade_broker_id(trade),
            client_order_id=(trade.order.orderRef or None) or None,
            status=self._map_ib_status(trade),
            symbol=sym,
            side=side,
            quantity=qty,
            filled_quantity=filled,
            avg_fill_price=avg_d,
            fee=self._trade_fee(trade),
            timestamp=_iso_now(),
        )

    def _find_trade_by_broker_id(self, broker_order_id: str) -> Optional[Trade]:
        if self._ib is None:
            return None
        for t in self._ib.trades():
            try:
                if self._trade_broker_id(t) == broker_order_id:
                    return t
            except Exception:  # noqa: BLE001
                # Defensive: stale Trades occasionally carry a non-Order object
                # in t.order (e.g. a Stock contract) which raises on orderId
                # access. Skip and keep scanning rather than aborting the loop.
                continue
        # Caller may hold broker_order_id from before permId arrived (was orderId string).
        for t in self._ib.trades():
            try:
                if str(t.order.orderId) == broker_order_id:
                    return t
            except Exception:  # noqa: BLE001
                continue
        return None

    def _find_trade_by_client_order_id(self, client_order_id: str) -> Optional[Trade]:
        if self._ib is None:
            return None
        for t in self._ib.trades():
            ref = t.order.orderRef or ""
            if ref == client_order_id:
                return t
        return None

    def order_cancel_diagnostics(self, broker_order_id: str) -> str:
        """
        Return IBKR trade log messages and advancedError text for an order.
        Used to distinguish market-hours / market-data issues from other rejects.
        """
        if self._ib is None:
            return ""
        trade = self._find_trade_by_broker_id(broker_order_id)
        if trade is None:
            return ""
        parts: list[str] = []
        if getattr(trade, "advancedError", None):
            parts.append(str(trade.advancedError))
        for entry in trade.log:
            if entry.message:
                parts.append(entry.message)
            elif entry.status:
                parts.append(entry.status)
        return " | ".join(parts)

    def _is_paxos_crypto(self, contract: Contract) -> bool:
        return (contract.secType or "").upper() == "CRYPTO" and (
            (contract.exchange or "").upper() == "PAXOS"
        )

    def _quote_symbol_for_contract(self, contract: Contract) -> str:
        """Symbol string used with get_last_price / stream (e.g. BTC/USD)."""
        if (contract.secType or "").upper() == "CRYPTO":
            return f"{contract.symbol}/{contract.currency}"
        return contract.symbol or ""

    async def _paxos_crypto_cash_notional_usd(
        self, order: Order, contract: Contract
    ) -> float:
        """
        PAXOS crypto orders require ``cashQty`` in USD (error 10289 if missing).
        ``order.quantity`` is treated as size in the base asset (e.g. BTC).
        """
        sym = self._quote_symbol_for_contract(contract)
        px = await self.get_last_price(sym if "/" in sym else order.symbol)
        if px <= 0:
            raise ValueError(
                f"PAXOS crypto order needs a live price to size cashQty; symbol={sym}"
            )
        base = abs(_d(order.quantity))
        usd = (base * px).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        return float(usd)

    async def _build_ib_order_for_contract(
        self, order: Order, contract: Contract
    ) -> object:
        """Build ib_insync order; PAXOS crypto uses cashQty with totalQuantity 0."""
        if self._is_paxos_crypto(contract):
            return await self._build_paxos_crypto_ib_order(order, contract)
        # IBKR forex (IDEALPRO) rejects fractional quantities with
        # error 10318: "This order doesn't support fractional quantity
        # trading". Sizing gives us e.g. ``75409.42964522`` units for a
        # GBP-denominated cash stake; IBKR requires whole base-currency
        # units. Round DOWN so we never exceed the sized notional.
        if isinstance(contract, Forex):
            qty_dec = Decimal(str(order.quantity))
            whole = qty_dec.to_integral_value(rounding=ROUND_DOWN)
            if whole != qty_dec and whole > 0:
                logger.info(
                    "place_order | IBKR | forex qty rounded to whole units | "
                    "symbol={} original={} rounded={}",
                    order.symbol,
                    qty_dec,
                    whole,
                )
                order.quantity = whole
        return self._build_ib_order(order)

    async def _build_paxos_crypto_ib_order(
        self, order: Order, contract: Contract
    ) -> object:
        action = "BUY" if order.side == OrderSide.BUY else "SELL"
        cash_usd = await self._paxos_crypto_cash_notional_usd(order, contract)
        if cash_usd <= 0:
            raise ValueError("PAXOS crypto cashQty must be positive")
        # IBKR: crypto MKT only allows IOC; crypto LMT allows IOC or Minutes (not GTC/DAY).
        # https://interactivebrokers.github.io/tws-api/cryptocurrency.html
        tif = (order.time_in_force or "").strip() or "GTC"
        if order.order_type == OrderType.MARKET:
            if tif.upper() != "IOC":
                logger.info(
                    "place_order | IBKR | PAXOS | MKT coercing TIF {!r} -> IOC "
                    "(IBKR crypto market orders only support IOC)",
                    tif,
                )
            tif = "IOC"
        else:
            tif_u = tif.upper()
            if tif_u in ("GTC", "DAY", "GTD", "OPG", "FOK", ""):
                logger.info(
                    "place_order | IBKR | PAXOS | LMT coercing TIF {!r} -> IOC "
                    "(IBKR crypto limit orders support IOC or Minutes only)",
                    tif,
                )
                tif = "IOC"
            elif tif_u == "MINUTES":
                tif = "Minutes"
        logger.info(
            "place_order | IBKR | PAXOS | cashQty_usd={} | base_qty={} | symbol={}",
            cash_usd,
            order.quantity,
            self._quote_symbol_for_contract(contract),
        )
        if order.order_type == OrderType.MARKET:
            ib_ord = MarketOrder(action, 0)
            ib_ord.cashQty = cash_usd
        elif order.order_type == OrderType.LIMIT:
            if order.limit_price is None:
                raise ValueError("limit_price required for LIMIT orders")
            ib_ord = LimitOrder(action, 0, float(order.limit_price))
            ib_ord.cashQty = cash_usd
        elif order.order_type == OrderType.STOP:
            if order.stop_price is None:
                raise ValueError("stop_price required for STOP orders")
            ib_ord = StopOrder(action, 0, float(order.stop_price))
            ib_ord.cashQty = cash_usd
        elif order.order_type == OrderType.STOP_LIMIT:
            if order.limit_price is None or order.stop_price is None:
                raise ValueError("limit_price and stop_price required for STOP_LIMIT orders")
            ib_ord = StopLimitOrder(
                action, 0, float(order.limit_price), float(order.stop_price)
            )
            ib_ord.cashQty = cash_usd
        else:
            raise ValueError(f"Unsupported order type: {order.order_type}")
        ib_ord.tif = tif
        if order.client_order_id:
            ib_ord.orderRef = order.client_order_id
        return ib_ord

    def _build_ib_order(self, order: Order) -> object:
        action = "BUY" if order.side == OrderSide.BUY else "SELL"
        qty = float(order.quantity)
        tif = order.time_in_force or "GTC"
        if order.order_type == OrderType.MARKET:
            ib_ord = MarketOrder(action, qty)
        elif order.order_type == OrderType.LIMIT:
            if order.limit_price is None:
                raise ValueError("limit_price required for LIMIT orders")
            ib_ord = LimitOrder(action, qty, float(order.limit_price))
        elif order.order_type == OrderType.STOP:
            if order.stop_price is None:
                raise ValueError("stop_price required for STOP orders")
            ib_ord = StopOrder(action, qty, float(order.stop_price))
        elif order.order_type == OrderType.STOP_LIMIT:
            if order.limit_price is None or order.stop_price is None:
                raise ValueError("limit_price and stop_price required for STOP_LIMIT orders")
            ib_ord = StopLimitOrder(
                action, qty, float(order.limit_price), float(order.stop_price)
            )
        else:
            raise ValueError(f"Unsupported order type: {order.order_type}")
        ib_ord.tif = tif
        if order.client_order_id:
            ib_ord.orderRef = order.client_order_id
        return ib_ord

    def _ticker_to_tick(self, symbol: str, ticker: object) -> Optional[Tick]:
        last = getattr(ticker, "last", None) or getattr(ticker, "close", None)
        bid = getattr(ticker, "bid", None)
        ask = getattr(ticker, "ask", None)
        if _is_bad_price(last):
            if not _is_bad_price(bid) and not _is_bad_price(ask):
                last = (float(bid) + float(ask)) / 2.0
            else:
                return None
        vol = getattr(ticker, "volume", None)
        if vol is None or (isinstance(vol, float) and vol != vol):
            ls = getattr(ticker, "lastSize", None)
            vol = ls if ls is not None and not (isinstance(ls, float) and ls != ls) else 0.0
        price = _d(last)
        if price.is_nan():
            return None
        bid_d = (
            _d(bid)
            if not _is_bad_price(bid)
            else None
        )
        ask_d = (
            _d(ask)
            if not _is_bad_price(ask)
            else None
        )
        vol_d = _d(vol)
        if vol_d.is_nan():
            vol_d = Decimal(0)
        return Tick(
            symbol=symbol,
            timestamp=_iso_now(),
            price=price,
            volume=vol_d,
            bid=bid_d,
            ask=ask_d,
        )

    def _post_connect_summary_probe_enabled(self) -> bool:
        v = (os.getenv("IBKR_POST_CONNECT_SUMMARY_PROBE", "1") or "1").strip().lower()
        return v not in ("0", "false", "no", "off")

    def _account_summary_tags(self) -> str:
        """
        Tags for ``reqAccountSummary``.

        ib_insync defaults include ``$LEDGER:ALL`` which can be slow or stall on
        some Gateway sessions; we keep a minimal default and allow opt-in.
        """
        v = (os.getenv("IBKR_ACCOUNT_SUMMARY_TAGS", "") or "").strip()
        if v:
            return v
        tags = "NetLiquidation,TotalCashValue,SettledCash,AvailableFunds"
        include_ledger = (os.getenv("IBKR_ACCOUNT_SUMMARY_INCLUDE_LEDGER", "") or "").strip().lower() in (
            "1",
            "true",
            "yes",
            "on",
        )
        if include_ledger:
            tags += ",$LEDGER:ALL"
        return tags

    async def _fetch_account_summary_rows(self, timeout: float) -> list[Any]:
        """
        Force a fresh account summary. ib_insync's ``accountSummaryAsync`` skips
        ``reqAccountSummaryAsync`` when ``acctSummary`` is non-empty, which can return
        stale or wrong-account rows; it can also mask a never-completed first request.
        """
        if self._ib is None or not self._ib.isConnected():
            return []
        try:
            to = float(timeout)
        except Exception:  # noqa: BLE001
            to = 12.0
        to = max(3.0, to)

        tags = self._account_summary_tags()
        self._ib.wrapper.acctSummary.clear()
        req_id = self._ib.client.getReqId()
        fut = self._ib.wrapper.startReq(req_id)
        self._ib.client.reqAccountSummary(req_id, "All", tags)

        # Wait for either completion or first rows; many Gateway builds only
        # emit accountSummaryEnd after cancel.
        loop = asyncio.get_running_loop()
        deadline = loop.time() + to
        while loop.time() < deadline and not fut.done():
            if self._ib.wrapper.acctSummary:
                break
            await asyncio.sleep(0.05)

        try:
            self._ib.client.cancelAccountSummary(req_id)
        except Exception as exc:  # noqa: BLE001
            logger.debug("cancelAccountSummary | {!r}", exc)

        # Give the end callback a chance to run; do not fail on timeout.
        try:
            await asyncio.wait_for(asyncio.shield(fut), timeout=3.0)
        except Exception:  # noqa: BLE001
            pass

        return list(self._ib.wrapper.acctSummary.values())

    async def _account_summary_probe_ok(self, timeout: float) -> bool:
        """True if a fresh reqAccountSummary completes with at least one row."""
        if self._ib is None or not self._ib.isConnected():
            return False
        try:
            rows = await self._fetch_account_summary_rows(timeout=max(3.0, timeout))
            return len(rows) > 0
        except Exception as exc:  # noqa: BLE001
            logger.debug("account summary probe | {!r}", exc)
            return False

    async def _connected_session_usable_after_connect_error(self, exc: Exception) -> bool:
        """
        Rescue a partially-synchronised IB session.

        ``ib_insync.connectAsync()`` can raise after the TCP/API handshake has
        completed if one of its startup sync requests stalls. In Gateway this
        still looks healthy: API Client connected, account values flowing, and
        positions/open orders replaying. Treat that as connected only after an
        account signal is visible, otherwise the broker manager stays red while
        a usable socket is already open.
        """
        if self._ib is None:
            return False
        try:
            if not self._ib.isConnected():
                return False
            accounts = self._ib.managedAccounts()
            if accounts or (self.account_id or "").strip():
                logger.warning(
                    "connect | IBKR | connectAsync raised after API session became usable; "
                    "proceeding | accounts={} | error={}",
                    accounts or "(explicit account configured)",
                    exc,
                )
                return True
            if await self._account_summary_probe_ok(timeout=5.0):
                logger.warning(
                    "connect | IBKR | connectAsync raised after account summary became usable; "
                    "proceeding | error={}",
                    exc,
                )
                return True
        except Exception as probe_exc:  # noqa: BLE001
            logger.debug(
                "connect | IBKR | partial-session rescue probe failed | original={!r} probe={!r}",
                exc,
                probe_exc,
            )
        return False

    async def connect(self) -> bool:
        """Establish connection to IB Gateway / TWS. Return True if successful."""
        self._last_connect_error = None
        try:
            if self._ib is None:
                self._ib = IB()
            if self._ib.isConnected():
                logger.info("connect | IBKR | already connected")
                return True
            # ib_insync runs reqAccountUpdatesMultiAsync per account when len(accounts) <= MaxSyncedSubAccounts.
            # That often triggers IB Error 321 ("Group name cannot be null") on simple paper logins.
            # Default 0 skips the multi-account sync block; set IBKR_MAX_SYNCED_SUB_ACCOUNTS=50 if you need FA-style subaccounts.
            try:
                IB.MaxSyncedSubAccounts = int(os.getenv("IBKR_MAX_SYNCED_SUB_ACCOUNTS", "0"))
            except ValueError:
                IB.MaxSyncedSubAccounts = 0
            # Same timeout applies to TCP handshake, each parallel sync task (positions, open
            # orders, account updates), and reqExecutionsAsync. Too low → "positions/account
            # updates timed out" while the socket still connects (degraded session). Default
            # 45s; override with IBKR_CONNECT_TIMEOUT.
            try:
                connect_timeout = float(os.getenv("IBKR_CONNECT_TIMEOUT", "45"))
            except ValueError:
                connect_timeout = 45.0
            connect_readonly = os.getenv("IBKR_CONNECT_READONLY", "").strip().lower() in (
                "1",
                "true",
                "yes",
                "on",
            )
            # Only pass account when set; empty string can confuse IB on some Gateway builds
            # (Error 321 / account sync). ib_insync auto-picks the sole managed account when omitted.
            acct = (self.account_id or "").strip()
            connect_kw: dict = {
                "host": self.host,
                "port": self.port,
                "clientId": self.client_id,
                "timeout": connect_timeout,
                "readonly": connect_readonly,
            }
            if acct:
                connect_kw["account"] = acct
            def _on_error(reqId: int, errorCode: int, errorString: str, contract: str) -> None:  # noqa: N803
                logger.warning(
                    "connect | IBKR | IB error | reqId={} code={} msg={} contract={}",
                    reqId, errorCode, errorString, contract,
                )

            self._ib.errorEvent += _on_error
            try:
                await self._ib.connectAsync(**connect_kw)
            finally:
                self._ib.errorEvent -= _on_error

            accounts = self._ib.managedAccounts()
            logger.info(
                "connect | IBKR | handshake done | accounts={} | clientId={}",
                accounts or "(none)", self.client_id,
            )
            if not accounts:
                logger.warning(
                    "connect | IBKR | no managed accounts after sync — reconnecting once "
                    "(degraded ib_insync sync is common right after Gateway starts)"
                )
                self._ib.disconnect()
                await asyncio.sleep(3.0)
                await self._ib.connectAsync(**connect_kw)
            if not self._ib.managedAccounts():
                # Avoid false negative when account summary is already alive or explicit account_id is set.
                if acct:
                    logger.warning(
                        "connect | IBKR | no managed accounts yet; proceeding with explicit account_id={}",
                        acct,
                    )
                elif await self._account_summary_probe_ok(timeout=max(5.0, connect_timeout)):
                    logger.warning(
                        "connect | IBKR | no managed accounts yet, but account summary is alive; proceeding",
                    )
                else:
                    self._last_connect_error = (
                        "No managed accounts after reconnect/account-summary probe"
                    )
                    logger.error(
                        "connect | IBKR | still no managed accounts after reconnect | "
                        "raise IBKR_CONNECT_TIMEOUT (e.g. 90–120) or wait for Gateway API"
                    )
                    self._force_close_ib()
                    return False

            if self._post_connect_summary_probe_enabled():
                try:
                    probe_to = float(os.getenv("IBKR_POST_CONNECT_SUMMARY_PROBE_TIMEOUT", "12"))
                except ValueError:
                    probe_to = 12.0
                probe_to = max(3.0, probe_to)
                if not await self._account_summary_probe_ok(probe_to):
                    logger.warning(
                        "connect | IBKR | account summary probe failed or empty — "
                        "reconnecting once with longer timeout (IBKR_CONNECT_BOOST_TIMEOUT)"
                    )
                    try:
                        boost = float(os.getenv("IBKR_CONNECT_BOOST_TIMEOUT", "90"))
                    except ValueError:
                        boost = 90.0
                    boost = max(connect_timeout, boost)
                    connect_kw_boost = {**connect_kw, "timeout": boost}
                    self._ib.disconnect()
                    await asyncio.sleep(2.0)
                    await self._ib.connectAsync(**connect_kw_boost)
                    if not self._ib.managedAccounts():
                        if acct:
                            logger.warning(
                                "connect | IBKR | no managed accounts after boosted reconnect; "
                                "proceeding with explicit account_id={}",
                                acct,
                            )
                        elif not await self._account_summary_probe_ok(probe_to):
                            self._last_connect_error = "No managed accounts after boosted reconnect"
                            logger.error(
                                "connect | IBKR | no managed accounts after boosted reconnect"
                            )
                            self._force_close_ib()
                            return False
                    if not await self._account_summary_probe_ok(probe_to):
                        logger.warning(
                            "connect | IBKR | account summary still failing after boosted "
                            "reconnect | get_balance may timeout — set IBKR_CONNECT_TIMEOUT "
                            "or IBKR_CONNECT_BOOST_TIMEOUT higher"
                        )

            if self.paper_mode:
                logger.info(
                    "connect | IBKR | session=paper | host={} | port={}",
                    self.host,
                    self.port,
                )
            else:
                logger.info(
                    "connect | IBKR | session=live | host={} | port={}",
                    self.host,
                    self.port,
                )
            return True
        except OSError as exc:
            self._last_connect_error = str(exc)[:200]
            logger.warning(
                "connect | IBKR | unreachable | host={} | port={} | error={} | "
                "start IB Gateway or TWS with API enabled on this port (paper=7497, live=7496)",
                self.host,
                self.port,
                exc,
            )
            self._force_close_ib()
            return False
        except Exception as exc:  # noqa: BLE001 — broker connectivity
            if await self._connected_session_usable_after_connect_error(exc):
                self._last_connect_error = None
                return True
            self._last_connect_error = str(exc)[:200]
            logger.exception("connect | IBKR | failed | error={}", exc)
            self._force_close_ib()
            return False

    def _force_close_ib(self) -> None:
        """Ensure the IB instance and its underlying socket are fully closed."""
        if self._ib is None:
            return
        try:
            if self._ib.isConnected():
                self._ib.disconnect()
            sock = getattr(self._ib.client, "_socket", None) or getattr(self._ib.client, "socket", None)
            if sock is not None:
                try:
                    sock.close()
                except Exception:
                    pass
        except Exception:
            pass
        self._ib = None

    async def disconnect(self) -> None:
        """Clean disconnect from IB Gateway / TWS, ensuring socket closure."""
        try:
            self._force_close_ib()
            logger.info("disconnect | IBKR | done")
        except Exception as exc:  # noqa: BLE001
            logger.warning("disconnect | IBKR | error={}", exc)
            self._ib = None
            self._last_ib_order_snapshot_monotonic = -1e9

    async def is_connected(self) -> bool:
        """Return True if the IB API connection is active."""
        try:
            return self._ib is not None and self._ib.isConnected()
        except Exception as exc:  # noqa: BLE001
            logger.warning("is_connected | IBKR | error={}", exc)
            return False

    async def get_balance(self) -> list[Balance]:
        """Return all account balances derived from IB account summary tags."""
        if self._ib is None or not self._ib.isConnected():
            logger.warning("get_balance | IBKR | not connected")
            return []
        try:
            acct = self._resolve_account()
            try:
                summary_timeout = float(os.getenv("IBKR_ACCOUNT_SUMMARY_TIMEOUT", "30"))
            except ValueError:
                summary_timeout = 30.0
            # Always issue a fresh summary (see _fetch_account_summary_rows); never block forever.
            try:
                rows = await self._fetch_account_summary_rows(
                    timeout=max(5.0, summary_timeout),
                )
                if acct:
                    filtered = [r for r in rows if r.account == acct]
                    if not filtered and rows:
                        logger.warning(
                            "get_balance | IBKR | no summary rows for account={!r} "
                            "(got {} other rows) — check IBKR_ACCOUNT_ID",
                            acct,
                            len(rows),
                        )
                    rows = filtered
            except asyncio.TimeoutError:
                logger.warning(
                    "get_balance | IBKR | account summary timed out after {}s | "
                    "raise IBKR_ACCOUNT_SUMMARY_TIMEOUT or fix Gateway SSO/network",
                    summary_timeout,
                )
                return []
            tags_by_ccy: dict[str, dict[str, Decimal]] = defaultdict(dict)
            for row in rows:
                if acct and row.account != acct:
                    continue
                if not row.currency or not str(row.currency).strip():
                    continue
                try:
                    tags_by_ccy[row.currency][row.tag] = _d(row.value)
                except Exception:  # noqa: BLE001
                    continue
            out: list[Balance] = []
            for ccy, tags in tags_by_ccy.items():
                total = _total_from_account_summary_tags(tags)
                available = tags.get("AvailableFunds") or tags.get("SettledCash") or total
                reserved = (total - available) if total > available else Decimal(0)
                out.append(
                    Balance(
                        currency=ccy,
                        total=total,
                        available=available,
                        reserved=reserved,
                    )
                )
            return out
        except Exception as exc:  # noqa: BLE001
            logger.exception("get_balance | IBKR | error={}", exc)
            return []

    async def get_positions(self) -> list[Position]:
        """Return open positions for the managed account."""
        if self._ib is None or not self._ib.isConnected():
            logger.warning("get_positions | IBKR | not connected")
            return []
        try:
            acct = self._resolve_account()
            await self._ib.reqPositionsAsync()
            port_map: dict[int, object] = {}
            for item in self._ib.portfolio():
                if acct and item.account != acct:
                    continue
                port_map[item.contract.conId] = item
            out: list[Position] = []
            for p in self._ib.positions():
                if acct and p.account != acct:
                    continue
                sym = self._contract_symbol_key(p.contract)
                qty = _d(p.position)
                if qty == 0:
                    continue
                pi = port_map.get(p.contract.conId)
                if pi is not None:
                    mpx = _d(pi.marketPrice)
                    unreal = _d(pi.unrealizedPNL)
                    avg_px = _d(pi.averageCost)
                else:
                    mpx = _d(p.avgCost)
                    unreal = Decimal(0)
                    avg_px = _d(p.avgCost)
                st = (p.contract.secType or "").upper()
                inst_meta: Optional[dict[str, Any]] = None
                if st == "OPT":
                    inst_meta = self._option_metadata_from_contract(p.contract)
                out.append(
                    Position(
                        symbol=sym,
                        asset_class=self._asset_class_from_contract(p.contract),
                        quantity=qty,
                        avg_entry_price=avg_px,
                        current_price=mpx,
                        unrealised_pnl=unreal,
                        broker=self.broker_name,
                        instrument_metadata=inst_meta,
                    )
                )
            return out
        except Exception as exc:  # noqa: BLE001
            logger.exception("get_positions | IBKR | error={}", exc)
            return []

    async def get_option_chain(self, underlying_symbol: str) -> list[dict[str, Any]]:
        """
        IBKR option parameter matrix for *underlying_symbol* (STK on SMART).

        Returns one dict per exchange slice:
        ``exchange``, ``underlying_con_id``, ``trading_class``, ``multiplier``,
        ``expirations`` (YYYYMMDD strings), ``strikes`` (Decimal).
        """
        if self._ib is None or not self._ib.isConnected():
            logger.warning("get_option_chain | IBKR | not connected")
            return []
        u = underlying_symbol.strip().upper()
        stock = Stock(u, "SMART", "USD")
        try:
            qualified = await self._ib.qualifyContractsAsync(stock)
            if not qualified:
                logger.warning("get_option_chain | IBKR | could not qualify underlying | {}", u)
                return []
            und = qualified[0]
            rows = await self._ib.reqSecDefOptParamsAsync(
                und.symbol,
                "",
                und.secType,
                und.conId,
            )
            out: list[dict[str, Any]] = []
            for ch in rows:
                expirations = [str(x).replace("-", "")[:8] for x in (ch.expirations or [])]
                strikes = [_d(s) for s in (ch.strikes or [])]
                mult = getattr(ch, "multiplier", None)
                try:
                    mult_i = int(float(mult)) if mult is not None else 100
                except Exception:  # noqa: BLE001
                    mult_i = 100
                out.append(
                    {
                        "exchange": str(getattr(ch, "exchange", "") or ""),
                        "underlying_con_id": int(getattr(ch, "underlyingConId", 0) or 0),
                        "trading_class": str(getattr(ch, "tradingClass", "") or ""),
                        "multiplier": mult_i,
                        "expirations": expirations,
                        "strikes": strikes,
                    }
                )
            return out
        except Exception as exc:  # noqa: BLE001
            logger.exception("get_option_chain | IBKR | symbol={} | error={}", u, exc)
            return []

    async def qualify_option_contract(self, spec: OptionContractSpec) -> Option:
        """Qualify a single-leg option; returns contract with ``conId`` / ``localSymbol`` set."""
        if self._ib is None or not self._ib.isConnected():
            raise ConnectionError("IBKR not connected")
        c = self.build_option_contract(spec)
        qualified = await self._ib.qualifyContractsAsync(c)
        if not qualified:
            raise ValueError(f"IBKR could not qualify option contract {spec!r}")
        return qualified[0]

    async def get_option_market_data(self, spec: OptionContractSpec) -> dict[str, Any]:
        """Snapshot bid / ask / last for a single option (requires market data permissions)."""
        if self._ib is None or not self._ib.isConnected():
            return {"bid": None, "ask": None, "last": None, "error": "not_connected"}
        c: Optional[Contract] = None
        try:
            c = await self.qualify_option_contract(spec)
            self._ib.reqMktData(c, "", True, False)
            await asyncio.sleep(1.2)
            t = self._ib.ticker(c)
            bid = getattr(t, "bid", None)
            ask = getattr(t, "ask", None)
            last = getattr(t, "last", None) or getattr(t, "close", None)
            out = {
                "bid": None if _is_bad_price(bid) else _d(bid),
                "ask": None if _is_bad_price(ask) else _d(ask),
                "last": None if _is_bad_price(last) else _d(last),
                "local_symbol": (getattr(c, "localSymbol", None) or "").strip(),
            }
            self._ib.cancelMktData(c)
            return out
        except Exception as exc:  # noqa: BLE001
            logger.exception("get_option_market_data | IBKR | error={}", exc)
            if c is not None and self._ib is not None:
                try:
                    self._ib.cancelMktData(c)
                except Exception:  # noqa: BLE001
                    pass
            return {"bid": None, "ask": None, "last": None, "error": str(exc)}

    async def get_last_price(self, symbol: str) -> Decimal:
        """Fetch last traded (or mid) price for a symbol via a snapshot request."""
        if self._ib is None or not self._ib.isConnected():
            logger.warning("get_last_price | IBKR | not connected | symbol={}", symbol)
            return Decimal(0)
        contract = self._symbol_to_contract(symbol)
        try:
            await self._ib.qualifyContractsAsync(contract)
            self._ib.reqMktData(contract, "", True, False)
            await asyncio.sleep(1.2)
            t = self._ib.ticker(contract)
            last = t.last or t.close
            if last is None or (isinstance(last, float) and last != last):
                if t.bid and t.ask:
                    last = (t.bid + t.ask) / 2.0
                else:
                    logger.warning("get_last_price | IBKR | no price | symbol={}", symbol)
                    return Decimal(0)
            self._ib.cancelMktData(contract)
            return _d(last)
        except Exception as exc:  # noqa: BLE001
            logger.exception("get_last_price | IBKR | symbol={} | error={}", symbol, exc)
            try:
                self._ib.cancelMktData(contract)
            except Exception:  # noqa: BLE001
                pass
            return Decimal(0)

    async def stream_prices(self, symbols: list[str]) -> AsyncIterator[Tick]:
        """
        Stream live ticks via reqMktData and IB pending ticker updates.
        Yields :class:`Tick` objects as bid/ask/last change.
        """
        if not symbols:
            logger.warning("stream_prices | IBKR | empty symbols list")
            return
        if self._ib is None or not self._ib.isConnected():
            logger.warning("stream_prices | IBKR | not connected")
            return

        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()
        conid_to_sym: dict[int, str] = {}
        contracts: list[Contract] = []
        subscribed = False

        def on_pending(tickers: Set[Ticker]) -> None:
            def _enqueue() -> None:
                for t in tickers:
                    try:
                        queue.put_nowait(t)
                    except Exception:  # noqa: BLE001
                        pass

            loop.call_soon_threadsafe(_enqueue)

        try:
            for sym in symbols:
                c = self._symbol_to_contract(sym)
                await self._ib.qualifyContractsAsync(c)
                conid_to_sym[c.conId] = sym.strip().upper()
                contracts.append(c)

            self._ib.pendingTickersEvent += on_pending
            subscribed = True
            for c in contracts:
                self._ib.reqMktData(c, "", False, False)

            while True:
                ticker = await queue.get()
                cid = ticker.contract.conId
                sym = conid_to_sym.get(cid, self._contract_symbol_key(ticker.contract))
                tick = self._ticker_to_tick(sym, ticker)
                if tick is not None:
                    yield tick
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("stream_prices | IBKR | error={}", exc)
        finally:
            try:
                if self._ib is not None:
                    if subscribed:
                        try:
                            self._ib.pendingTickersEvent -= on_pending
                        except Exception:  # noqa: BLE001
                            pass
                    for c in contracts:
                        try:
                            self._ib.cancelMktData(c)
                        except Exception:  # noqa: BLE001
                            pass
            except Exception as exc:  # noqa: BLE001
                logger.warning("stream_prices | IBKR | cleanup | error={}", exc)

    async def get_candles(
        self, symbol: str, timeframe: str, limit: int = 200
    ) -> list[Candle]:
        """Fetch historical OHLCV bars (TRADES) and map them to :class:`Candle`."""
        if self._ib is None or not self._ib.isConnected():
            logger.warning("get_candles | IBKR | not connected")
            return []
        bar_size = self._BAR_SIZE.get(timeframe)
        if not bar_size:
            logger.warning("get_candles | IBKR | unsupported timeframe={}", timeframe)
            return []
        contract = self._symbol_to_contract(symbol)
        try:
            await self._ib.qualifyContractsAsync(contract)
            use_rth = (contract.secType or "").upper() == "STK"
            duration = self._duration_str(timeframe, limit)
            bars = await self._ib.reqHistoricalDataAsync(
                contract,
                endDateTime="",
                durationStr=duration,
                barSizeSetting=bar_size,
                whatToShow="TRADES",
                useRTH=use_rth,
                formatDate=1,
            )
            sym_key = self._contract_symbol_key(contract)
            out: list[Candle] = []
            slice_bars = bars[-limit:] if len(bars) > limit else bars
            for bar in slice_bars:
                bd = bar.date
                if isinstance(bd, datetime):
                    bdt = bd if bd.tzinfo else bd.replace(tzinfo=timezone.utc)
                    ts = bdt.isoformat().replace("+00:00", "Z")
                elif isinstance(bd, date):
                    ts = f"{bd.isoformat()}T00:00:00Z"
                else:
                    ts = str(bd)
                out.append(
                    Candle(
                        symbol=sym_key,
                        timestamp=ts,
                        open=_d(bar.open),
                        high=_d(bar.high),
                        low=_d(bar.low),
                        close=_d(bar.close),
                        volume=_d(bar.volume),
                        timeframe=timeframe,
                    )
                )
            return out
        except Exception as exc:  # noqa: BLE001
            logger.exception("get_candles | IBKR | symbol={} | error={}", symbol, exc)
            return []

    async def place_order(self, order: Order) -> OrderResult:
        """
        Place an order via IB. Idempotent when ``client_order_id`` matches
        an existing tracked order (orderRef).
        """
        if self._ib is None or not self._ib.isConnected():
            logger.error("place_order | IBKR | not connected")
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
        if self.paper_mode:
            logger.info(
                "place_order | IBKR | paper_mode | routing to IB paper | symbol={} | side={}",
                order.symbol,
                order.side.value,
            )
        try:
            if order.client_order_id:
                existing = self._find_trade_by_client_order_id(order.client_order_id)
                if existing is not None:
                    logger.info(
                        "place_order | IBKR | idempotent | client_order_id={}",
                        order.client_order_id,
                    )
                    return self._trade_to_order_result(existing)

            contract = self._order_to_contract(order)
            await self._ib.qualifyContractsAsync(contract)
            ib_ord = await self._build_ib_order_for_contract(order, contract)
            trade = self._ib.placeOrder(contract, ib_ord)
            for _ in range(120):
                await asyncio.sleep(0.05)
                if trade.order.permId or trade.orderStatus.status not in (
                    "PendingSubmit",
                    "ApiPending",
                ):
                    break
            logger.info(
                "place_order | IBKR | broker_order_id={} | status={}",
                self._trade_broker_id(trade),
                trade.orderStatus.status,
            )
            return self._trade_to_order_result(trade)
        except Exception as exc:  # noqa: BLE001
            logger.exception("place_order | IBKR | error={}", exc)
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
        """Cancel an open order by broker id (permId preferred, else orderId)."""
        if self._ib is None or not self._ib.isConnected():
            logger.warning("cancel_order | IBKR | not connected")
            return False
        try:
            trade = self._find_trade_by_broker_id(broker_order_id)
            if trade is None:
                logger.warning("cancel_order | IBKR | not found | id={}", broker_order_id)
                return False
            self._ib.cancelOrder(trade.order)
            logger.info("cancel_order | IBKR | id={}", broker_order_id)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.exception("cancel_order | IBKR | id={} | error={}", broker_order_id, exc)
            return False

    def _trade_ib_is_terminal(self, trade: Trade) -> bool:
        s = trade.orderStatus.status or ""
        if s in ("Filled", "Cancelled", "ApiCancelled", "Inactive"):
            return True
        qty = _d(trade.order.totalQuantity)
        filled_e = self._effective_filled_qty(trade)
        rem = self._remaining_safe(trade)
        if qty > 0 and filled_e >= qty:
            return True
        if qty == 0 and filled_e > 0 and rem <= 0:
            return True
        return False

    async def _refresh_ib_order_snapshot(self, *, force: bool = False) -> None:
        """Replay open orders + executions so Trade matches Gateway (helps after connect sync timeouts)."""
        if self._ib is None or not self._ib.isConnected():
            return
        try:
            min_iv = float(os.getenv("IBKR_ORDER_REFRESH_MIN_SEC", "2"))
        except ValueError:
            min_iv = 2.0
        min_iv = max(0.0, min_iv)
        now = asyncio.get_running_loop().time()
        async with self._ib_order_snap_lock:
            if (
                not force
                and self._last_ib_order_snapshot_monotonic > -1e8
                and (now - self._last_ib_order_snapshot_monotonic) < min_iv
            ):
                return
            try:
                to = float(os.getenv("IBKR_ORDER_REFRESH_TIMEOUT", "15"))
            except ValueError:
                to = 15.0
            to = max(3.0, to)
            try:
                await asyncio.wait_for(self._ib.reqOpenOrdersAsync(), timeout=to)
            except Exception as exc:  # noqa: BLE001
                logger.debug("reqOpenOrdersAsync | {}", exc)
            try:
                await asyncio.wait_for(self._ib.reqExecutionsAsync(), timeout=to)
            except Exception as exc:  # noqa: BLE001
                logger.debug("reqExecutionsAsync | {}", exc)
            self._last_ib_order_snapshot_monotonic = asyncio.get_running_loop().time()

    async def get_order(self, broker_order_id: str) -> OrderResult:
        """Return current status for an order by broker id."""
        if self._ib is None or not self._ib.isConnected():
            logger.warning("get_order | IBKR | not connected")
            raise ConnectionError("IBKR not connected")
        trade = self._find_trade_by_broker_id(broker_order_id)
        if trade is None:
            logger.warning("get_order | IBKR | not found | id={}", broker_order_id)
            raise ValueError(f"Order not found: {broker_order_id}")
        if not self._trade_ib_is_terminal(trade):
            await self._refresh_ib_order_snapshot()
            trade = self._find_trade_by_broker_id(broker_order_id) or trade
        return self._trade_to_order_result(trade)

    async def get_open_orders(self) -> list[OrderResult]:
        """Return all orders that are still active at IB."""
        if self._ib is None or not self._ib.isConnected():
            logger.warning("get_open_orders | IBKR | not connected")
            return []
        try:
            return [self._trade_to_order_result(t) for t in self._ib.openTrades()]
        except Exception as exc:  # noqa: BLE001
            logger.exception("get_open_orders | IBKR | error={}", exc)
            return []

    async def get_order_book(self, symbol: str, depth: int = 10) -> OrderBook:
        """
        Snapshot order book via market depth (DOM). IB returns at most five levels
        per side unless using smart depth.
        """
        if self._ib is None or not self._ib.isConnected():
            logger.warning("get_order_book | IBKR | not connected")
            return OrderBook(symbol=symbol, timestamp=_iso_now(), bids=[], asks=[])
        rows = min(depth, 5)
        contract = self._symbol_to_contract(symbol)
        try:
            await self._ib.qualifyContractsAsync(contract)
            self._ib.reqMktDepth(contract, numRows=rows, isSmartDepth=False)
            await asyncio.sleep(1.5)
            t = self._ib.ticker(contract)
            bids = [
                (_d(x.price), _d(x.size)) for x in list(t.domBids)[:rows]
            ]
            asks = [
                (_d(x.price), _d(x.size)) for x in list(t.domAsks)[:rows]
            ]
            self._ib.cancelMktDepth(contract)
            return OrderBook(
                symbol=symbol,
                timestamp=_iso_now(),
                bids=bids,
                asks=asks,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("get_order_book | IBKR | symbol={} | error={}", symbol, exc)
            try:
                self._ib.cancelMktDepth(contract)
            except Exception:  # noqa: BLE001
                pass
            return OrderBook(symbol=symbol, timestamp=_iso_now(), bids=[], asks=[])

    async def get_supported_symbols(self) -> list[str]:
        """IBKR has no single list-all API; return an empty list."""
        logger.debug("get_supported_symbols | IBKR | not supported via API")
        return []

    async def get_asset_class(self, symbol: str) -> AssetClass:
        """Best-effort asset class from symbol string (contract not qualified)."""
        s = symbol.strip().upper()
        if s in _KNOWN_PAXOS_CRYPTO:
            return AssetClass.CRYPTO
        if "/" in s:
            base = s.split("/", 1)[0].strip()
            if base in _KNOWN_PAXOS_CRYPTO:
                return AssetClass.CRYPTO
        if len(s) == 6 and s.isalpha():
            return AssetClass.FOREX
        return AssetClass.EQUITY
