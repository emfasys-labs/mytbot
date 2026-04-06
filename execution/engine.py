"""
execution/engine.py
====================
The Execution Engine.

Receives an APPROVED signal from the Risk Engine.
Translates it into an Order.
Routes it to the correct broker via smart order routing.
Tracks the fill.
Logs everything.

Key properties:
- Idempotent: uses client_order_id to prevent duplicate orders on retry
- Reconciles: checks broker state vs internal state periodically
- Paper-aware: in paper mode, simulates fills without hitting broker
"""

import uuid
import logging
import os
from decimal import Decimal
from typing import Optional

import httpx
from brokers.registry import get_broker
from control.runtime import get_risk_engine, set_execution_engine
from brokers.base import Order, OrderBook, OrderResult, OrderSide, OrderStatus, OrderType, Position
from risk.engine import Signal, RiskDecision, RiskVerdict

logger = logging.getLogger(__name__)


class ExecutionEngine:

    def __init__(
        self,
        broker_configs: dict,
        paper_mode: bool = True,
        *,
        place_order_retries: int = 2,
        place_order_retry_backoff_sec: float = 1.0,
        fill_poll_timeout_sec: float = 10.0,
        fill_poll_interval_sec: float = 1.0,
        cancel_partial_on_timeout: bool = True,
    ):
        self.paper_mode = paper_mode
        self.broker_configs = broker_configs
        self._brokers = {}          # lazy-loaded broker adapters
        self._open_orders = {}      # client_order_id → OrderResult
        self.place_order_retries = max(0, int(place_order_retries))
        self.place_order_retry_backoff_sec = float(place_order_retry_backoff_sec)
        self.fill_poll_timeout_sec = float(fill_poll_timeout_sec)
        self.fill_poll_interval_sec = float(fill_poll_interval_sec)
        self.cancel_partial_on_timeout = bool(cancel_partial_on_timeout)
        set_execution_engine(self)

    async def execute(
        self,
        signal: Signal,
        risk_decision: RiskDecision,
        *,
        session_factory=None,
    ) -> Optional[OrderResult]:
        """
        Execute an approved signal.
        Returns OrderResult on success, None on failure.
        """

        if risk_decision.verdict != RiskVerdict.APPROVED:
            logger.warning(f"Attempted to execute rejected signal {signal.signal_id}")
            return None

        broker = await self._get_broker(signal.broker)
        if broker is None:
            logger.error("Broker unavailable | signal_id=%s broker=%s", signal.signal_id, signal.broker)
            await self._send_critical_alert(
                f"Broker unavailable for signal {signal.signal_id} ({signal.symbol}) on {signal.broker}"
            )
            return None
        order  = self._build_order(signal)

        logger.info(
            f"EXECUTING | {signal.symbol} {signal.side} "
            f"qty={signal.suggested_quantity} | "
            f"broker={signal.broker} | "
            f"mode={'PAPER' if self.paper_mode else 'LIVE'}"
        )

        if not await self._passes_execution_limits(broker, order):
            logger.warning(
                "Execution pre-check rejected | signal_id=%s symbol=%s broker=%s",
                signal.signal_id,
                signal.symbol,
                signal.broker,
            )
            return None

        result: Optional[OrderResult] = None
        for attempt in range(self.place_order_retries + 1):
            try:
                result = await broker.place_order(order)
                break
            except Exception as e:
                logger.error(
                    "Order placement failed | signal_id=%s | attempt=%s/%s | %s",
                    signal.signal_id,
                    attempt + 1,
                    self.place_order_retries + 1,
                    e,
                )
                if attempt < self.place_order_retries:
                    await self._reconnect_broker(signal.broker)
                    if self.place_order_retry_backoff_sec > 0:
                        import asyncio

                        await asyncio.sleep(self.place_order_retry_backoff_sec * (attempt + 1))
                    continue
                self._maybe_auto_kill("place_order failure")
                await self._send_critical_alert(
                    f"Order placement failed for signal {signal.signal_id} ({signal.symbol})"
                )
                return None

        if result is None:
            return None

        self._open_orders[order.client_order_id] = result
        tracked = await self._track_fill_status(broker, result)
        if tracked is not None:
            result = tracked
            self._open_orders[order.client_order_id] = tracked

        logger.info("ORDER PLACED | %s | status=%s", result.broker_order_id, result.status)

        if session_factory is not None:
            try:
                from storage.db import persist_order_log

                await persist_order_log(
                    session_factory,
                    order=order,
                    result=result,
                    signal_id=signal.signal_id,
                    paper_mode=self.paper_mode,
                    broker=signal.broker,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("Order log persistence failed | signal_id=%s | %s", signal.signal_id, exc)
        return result

    async def cancel_all(self) -> None:
        """Emergency: cancel all open orders across all brokers."""
        logger.warning("CANCELLING ALL OPEN ORDERS")
        for broker_name, broker in self._brokers.items():
            try:
                open_orders = await broker.get_open_orders()
                for order in open_orders:
                    await broker.cancel_order(order.broker_order_id)
                    logger.info(f"Cancelled {order.broker_order_id} on {broker_name}")
            except Exception as e:
                logger.error(f"Failed to cancel orders on {broker_name}: {e}")

    async def reconcile_positions(self, *, max_quantity_diff: Decimal = Decimal("0.000001")) -> bool:
        """
        Compare broker-reported positions against latest local snapshot.
        Returns True when consistent; False when mismatch/failure.
        """
        try:
            ok = await self._reconcile_positions_internal(max_quantity_diff=max_quantity_diff)
        except Exception as exc:  # noqa: BLE001
            logger.error("Position reconciliation failed | %s", exc)
            self._maybe_auto_kill_reconciliation("reconciliation exception")
            return False
        if not ok:
            self._maybe_auto_kill_reconciliation("position mismatch")
        return ok

    def _build_order(self, signal: Signal) -> Order:
        return Order(
            symbol=signal.symbol,
            side=OrderSide.BUY if signal.side == "buy" else OrderSide.SELL,
            order_type=OrderType.MARKET if signal.suggested_price is None else OrderType.LIMIT,
            quantity=signal.suggested_quantity,
            limit_price=signal.suggested_price,
            client_order_id=str(uuid.uuid4()),  # idempotency key
        )

    async def _get_broker(self, name: str):
        """Lazy-load broker adapter."""
        if name not in self._brokers:
            config = self.broker_configs.get(name, {})
            broker = get_broker(
                name,
                paper_mode=self.paper_mode,
                **config
            )
            connected = await broker.connect()
            if not connected:
                logger.error("Broker connect failed | broker=%s", name)
                self._maybe_auto_kill("broker connect failure")
                return None
            self._brokers[name] = broker
        return self._brokers[name]

    async def _reconnect_broker(self, name: str) -> bool:
        broker = self._brokers.get(name)
        if broker is None:
            broker = await self._get_broker(name)
            return broker is not None
        try:
            connected = await broker.is_connected()
        except Exception:  # noqa: BLE001
            connected = False
        if connected:
            return True
        try:
            return bool(await broker.connect())
        except Exception as exc:  # noqa: BLE001
            logger.warning("Broker reconnect failed | broker=%s | %s", name, exc)
            return False

    def _execution_limits(self) -> dict:
        # Source limits from active risk engine config when available.
        risk_engine = get_risk_engine()
        cfg = getattr(risk_engine, "config", {}) if risk_engine is not None else {}
        return {
            "max_spread_pct": Decimal(str(cfg.get("max_spread_pct", "1.0"))),
            "min_liquidity_usd": Decimal(str(cfg.get("min_liquidity_usd", "0"))),
            "max_slippage_pct": Decimal(str(cfg.get("max_slippage_pct", "1.0"))),
            "auto_kill_on_api_failure": bool(cfg.get("auto_kill_on_api_failure", False)),
            "auto_kill_on_reconciliation_failure": bool(cfg.get("auto_kill_on_reconciliation_failure", False)),
        }

    async def _passes_execution_limits(self, broker, order: Order) -> bool:
        limits = self._execution_limits()
        try:
            ob: OrderBook = await broker.get_order_book(order.symbol, depth=10)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Order book fetch failed | symbol=%s | %s", order.symbol, exc)
            self._maybe_auto_kill("order book fetch failure")
            return False

        best_bid = ob.bids[0][0] if ob.bids else Decimal("0")
        best_ask = ob.asks[0][0] if ob.asks else Decimal("0")
        if best_bid <= 0 or best_ask <= 0:
            logger.warning("Invalid order book top-of-book | symbol=%s", order.symbol)
            return False

        mid = (best_bid + best_ask) / Decimal("2")
        spread_pct = (best_ask - best_bid) / mid if mid > 0 else Decimal("1")
        if spread_pct > limits["max_spread_pct"]:
            logger.warning(
                "Spread limit breach | symbol=%s spread_pct=%s max=%s",
                order.symbol,
                spread_pct,
                limits["max_spread_pct"],
            )
            return False

        book_liquidity = self._book_liquidity_usd(ob)
        if book_liquidity < limits["min_liquidity_usd"]:
            logger.warning(
                "Liquidity limit breach | symbol=%s liquidity=%s min=%s",
                order.symbol,
                book_liquidity,
                limits["min_liquidity_usd"],
            )
            return False

        if order.order_type == OrderType.MARKET:
            slippage_pct = self._estimate_market_slippage_pct(order, ob, mid)
            if slippage_pct > limits["max_slippage_pct"]:
                logger.warning(
                    "Slippage limit breach | symbol=%s slippage_pct=%s max=%s",
                    order.symbol,
                    slippage_pct,
                    limits["max_slippage_pct"],
                )
                return False

        return True

    @staticmethod
    def _book_liquidity_usd(order_book: OrderBook) -> Decimal:
        total = Decimal("0")
        for price, size in order_book.bids:
            total += price * size
        for price, size in order_book.asks:
            total += price * size
        return total

    @staticmethod
    def _estimate_market_slippage_pct(order: Order, order_book: OrderBook, mid: Decimal) -> Decimal:
        levels = order_book.asks if order.side == OrderSide.BUY else order_book.bids
        needed = abs(order.quantity)
        if needed <= 0 or not levels or mid <= 0:
            return Decimal("1")

        filled = Decimal("0")
        notional = Decimal("0")
        for px, sz in levels:
            if filled >= needed:
                break
            take = min(needed - filled, sz)
            filled += take
            notional += take * px
        if filled < needed:
            return Decimal("1")
        avg_fill = notional / filled
        return abs(avg_fill - mid) / mid

    async def _track_fill_status(self, broker, result: OrderResult) -> Optional[OrderResult]:
        broker_order_id = result.broker_order_id
        if not broker_order_id:
            return result

        try:
            import asyncio

            waited = 0.0
            last_partial: Optional[OrderResult] = None
            while waited < self.fill_poll_timeout_sec:
                latest = await broker.get_order(broker_order_id)
                if latest.status in {OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED}:
                    return latest
                if latest.status == OrderStatus.PARTIALLY_FILLED:
                    # Keep polling for terminal state and preserve latest partial snapshot.
                    last_partial = latest
                    result = latest
                await asyncio.sleep(max(0.1, self.fill_poll_interval_sec))
                waited += max(0.1, self.fill_poll_interval_sec)
            if last_partial is not None and self.cancel_partial_on_timeout:
                try:
                    await broker.cancel_order(broker_order_id)
                    final = await broker.get_order(broker_order_id)
                    return final
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "Partial fill timeout; cancel remainder failed | broker_order_id=%s | %s",
                        broker_order_id,
                        exc,
                    )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Fill tracking failed | broker_order_id=%s | %s", broker_order_id, exc)
            return result
        return result

    def _maybe_auto_kill(self, reason: str) -> None:
        limits = self._execution_limits()
        if not limits["auto_kill_on_api_failure"]:
            return
        risk_engine = get_risk_engine()
        if risk_engine is None:
            return
        try:
            risk_engine.kill()
            logger.critical("Auto-kill triggered by execution failure: %s", reason)
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to auto-kill risk engine: %s", exc)

    async def _reconcile_positions_internal(self, *, max_quantity_diff: Decimal) -> bool:
        from sqlalchemy import func, select
        from storage.models import PositionLog

        engine, session_factory = await self._init_db()
        if session_factory is None:
            logger.warning("Position reconciliation skipped | DB unavailable")
            return True
        try:
            local: dict[tuple[str, str], Decimal] = {}
            async with session_factory() as session:
                latest_ts_q = await session.execute(select(func.max(PositionLog.timestamp)))
                latest_ts = latest_ts_q.scalar_one_or_none()
                if latest_ts is not None:
                    rows_q = await session.execute(
                        select(PositionLog).where(PositionLog.timestamp == latest_ts)
                    )
                    rows = list(rows_q.scalars().all())
                    for row in rows:
                        key = (str(row.broker).strip().lower(), str(row.symbol).strip().upper())
                        local[key] = local.get(key, Decimal("0")) + Decimal(str(row.quantity))

            remote: dict[tuple[str, str], Decimal] = {}
            for broker_name, broker in self._brokers.items():
                try:
                    positions: list[Position] = await broker.get_positions()
                except Exception as exc:  # noqa: BLE001
                    logger.error("Broker positions fetch failed | broker=%s | %s", broker_name, exc)
                    return False
                for p in positions:
                    key = (broker_name.strip().lower(), str(p.symbol).strip().upper())
                    remote[key] = remote.get(key, Decimal("0")) + Decimal(str(p.quantity))

            keys = set(local.keys()) | set(remote.keys())
            for key in keys:
                lq = local.get(key, Decimal("0"))
                rq = remote.get(key, Decimal("0"))
                if abs(lq - rq) > max_quantity_diff:
                    logger.error(
                        "Position mismatch | broker=%s symbol=%s local_qty=%s remote_qty=%s",
                        key[0],
                        key[1],
                        lq,
                        rq,
                    )
                    return False
            return True
        finally:
            await self._dispose_db(engine)

    def _maybe_auto_kill_reconciliation(self, reason: str) -> None:
        limits = self._execution_limits()
        if not limits["auto_kill_on_reconciliation_failure"]:
            return
        risk_engine = get_risk_engine()
        if risk_engine is None:
            return
        try:
            risk_engine.kill()
            logger.critical("Auto-kill triggered by reconciliation failure: %s", reason)
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to auto-kill risk engine on reconciliation failure: %s", exc)

    @staticmethod
    async def _init_db():
        from storage.db import init_async_database

        return await init_async_database()

    @staticmethod
    async def _dispose_db(engine) -> None:
        from storage.db import dispose_engine

        await dispose_engine(engine)

    async def _send_critical_alert(self, message: str) -> None:
        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
        if not token or not chat_id:
            return
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = {"chat_id": chat_id, "text": f"[mytbot] {message}"}
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                await client.post(url, json=payload)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Telegram alert failed | %s", exc)
