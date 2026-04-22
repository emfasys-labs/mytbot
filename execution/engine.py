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
import random
from dataclasses import replace
from decimal import Decimal, ROUND_DOWN
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
import asyncio

import httpx
from brokers.registry import get_broker
from control.runtime import get_risk_engine, set_execution_engine
from brokers.base import Order, OrderBook, OrderResult, OrderSide, OrderStatus, OrderType, Position
from core.instruments import parse_option_contract_from_metadata
from risk.engine import Signal, RiskDecision, RiskVerdict

from execution.arbitrage_executor import ArbitrageExecutor
from execution.arbitrage_spot_executor import SpotArbitrageExecutor

logger = logging.getLogger(__name__)


class ExecutionEngine:

    def __init__(
        self,
        broker_configs: dict,
        paper_mode: bool = True,
        *,
        allowed_brokers: list[str] | None = None,
        broker_manager: Any | None = None,
        place_order_retries: int = 2,
        place_order_retry_backoff_sec: float = 1.0,
        fill_poll_timeout_sec: float = 10.0,
        fill_poll_interval_sec: float = 1.0,
        cancel_partial_on_timeout: bool = True,
    ):
        self.paper_mode = paper_mode
        self.broker_configs = broker_configs
        self.allowed_brokers = [b.strip().lower() for b in (allowed_brokers or []) if str(b).strip()]
        self._broker_manager = broker_manager
        self._brokers = {}          # lazy-loaded broker adapters
        self._open_orders = {}      # client_order_id → OrderResult
        self.place_order_retries = max(0, int(place_order_retries))
        self.place_order_retry_backoff_sec = float(place_order_retry_backoff_sec)
        self.fill_poll_timeout_sec = float(fill_poll_timeout_sec)
        self.fill_poll_interval_sec = float(fill_poll_interval_sec)
        self.cancel_partial_on_timeout = bool(cancel_partial_on_timeout)
        # In-flight dedup — when the allocator re-ranks the same opportunity
        # on consecutive loops, we must not flood the broker with duplicate
        # limit orders that stack up unfilled. The dedup query already filters
        # on status in {pending, open, partially_filled}, which are inherently
        # "working at the broker"; the time window is only a backstop for stale
        # DB rows a reconciliation bug might otherwise orphan. A 7-day window
        # covers weekend-held orders and broker re-connect lag without ever
        # allowing a second copy of a still-live order to be submitted.
        # Tunable via EXECUTION_DEDUP_WINDOW_SEC (default 604800s = 7 days).
        try:
            self.dedup_window_sec = float(os.getenv("EXECUTION_DEDUP_WINDOW_SEC", "604800") or 0)
        except (TypeError, ValueError):
            self.dedup_window_sec = 604800.0
        self.dedup_skipped = 0  # observability counter
        # Marketable-limit slippage buffer. Every LIMIT order's price is
        # rewritten just before placement so BUYs sit at or above the current
        # ask (and SELLs at or below the current bid), making them likely to
        # fill the top of book immediately. This prevents the 1h-old bar
        # close from becoming an unmarketable, stuck-in-the-queue bid.
        # Tunable via EXECUTION_MARKETABLE_SLIP_BPS (default 10 bps).
        # Set to 0 to disable and keep legacy "limit at last close" behaviour.
        try:
            self.marketable_slip_bps = float(os.getenv("EXECUTION_MARKETABLE_SLIP_BPS", "10") or 0)
        except (TypeError, ValueError):
            self.marketable_slip_bps = 10.0
        self.marketable_adjusted = 0  # observability counter
        set_execution_engine(self)

    def add_allowed_broker(self, name: str) -> None:
        """Register a venue that became available after engine construction (e.g. late IBKR connect)."""
        n = (name or "").strip().lower()
        if not n or n in self.allowed_brokers:
            return
        self.allowed_brokers.append(n)

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
        In paper mode: simulates a fill if the broker is unavailable or
        execution pre-checks fail, so the signal still produces a visible order.
        """

        if risk_decision.verdict != RiskVerdict.APPROVED:
            logger.warning(f"Attempted to execute rejected signal {signal.signal_id}")
            return None

        if (signal.side or "").strip().upper().startswith("ARBITRAGE_"):
            return await self._execute_arbitrage(signal, session_factory=session_factory)

        # Dedup: if a non-terminal order for (symbol, side, broker) already
        # exists within ``dedup_window_sec``, skip emitting a duplicate. This
        # prevents the allocator from re-submitting the same opportunity each
        # loop iteration when a prior limit order is still sitting unfilled.
        if self.dedup_window_sec > 0:
            existing = await self._find_in_flight_order(session_factory, signal)
            if existing is not None:
                self.dedup_skipped += 1
                logger.info(
                    "DEDUP SKIP | %s %s broker=%s (existing order %s status=%s qty=%s age=%ss)",
                    signal.symbol,
                    signal.side,
                    signal.broker,
                    existing.id,
                    existing.status,
                    existing.quantity,
                    int((datetime.now(timezone.utc) - existing.timestamp).total_seconds())
                    if existing.timestamp else -1,
                )
                return None

        order = self._build_order(signal)

        broker = await self._get_broker(signal.broker)
        if broker is None:
            if self.paper_mode:
                logger.info(
                    "PAPER FILL (no broker) | %s %s qty=%s broker=%s",
                    signal.symbol, signal.side, signal.suggested_quantity, signal.broker,
                )
                result = await self._simulate_fill(order, signal, broker=None)
                await self._persist_result(session_factory, order, result, signal)
                return result
            logger.error("Broker unavailable | signal_id=%s broker=%s", signal.signal_id, signal.broker)
            await self._send_critical_alert(
                f"Broker unavailable for signal {signal.signal_id} ({signal.symbol}) on {signal.broker}"
            )
            return None

        order = await self._apply_marketable_limit(order, signal, broker)
        order = self._normalize_order_for_broker(order, signal)
        if order.quantity <= 0:
            logger.warning(
                "Execution quantity invalid after broker normalization | signal_id=%s symbol=%s broker=%s qty=%s",
                signal.signal_id,
                signal.symbol,
                signal.broker,
                order.quantity,
            )
            return None

        # D031C — execution-boundary sanity guard. Reject loudly if the order
        # notional about to hit the broker materially exceeds the coordinator's
        # intended final capital. This is a defensive backstop, not the primary
        # sizing mechanism; normal flows never trigger it.
        # Telegram notification is intentionally suppressed: this path can fire
        # per-signal across a batch and would spam the channel. Operators watch
        # the `SIZING GUARD REJECT` CRITICAL logs instead.
        if not self._passes_sizing_boundary_guard(order, signal):
            return None

        await self._publish_symbol_constraints(signal, broker)

        logger.info(
            "EXECUTING | %s %s qty=%s | broker=%s | mode=%s",
            signal.symbol, signal.side, signal.suggested_quantity,
            signal.broker, "PAPER" if self.paper_mode else "LIVE",
        )

        if not await self._passes_execution_limits(broker, order, broker_name=str(signal.broker or "").strip().lower()):
            if self.paper_mode:
                logger.info(
                    "PAPER FILL (limits bypassed) | %s %s qty=%s",
                    signal.symbol, signal.side, signal.suggested_quantity,
                )
                result = await self._simulate_fill(order, signal, broker=broker)
                await self._persist_result(session_factory, order, result, signal)
                return result
            logger.warning(
                "Execution pre-check rejected | signal_id=%s symbol=%s broker=%s",
                signal.signal_id, signal.symbol, signal.broker,
            )
            return None

        result: Optional[OrderResult] = None
        for attempt in range(self.place_order_retries + 1):
            try:
                result = await broker.place_order(order)
                if result is None:
                    raise RuntimeError("broker.place_order returned None")
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
                    delay = 0.0
                    if self.place_order_retry_backoff_sec > 0:
                        delay = self.place_order_retry_backoff_sec * (attempt + 1)
                    bname = str(signal.broker or "").strip().lower()
                    if bname == "ibkr":
                        try:
                            jmax = float(os.getenv("IBKR_PLACE_ORDER_RETRY_JITTER_SEC", "0.5"))
                        except ValueError:
                            jmax = 0.5
                        if jmax > 0:
                            delay += random.uniform(0.0, jmax)
                    if delay > 0:
                        await asyncio.sleep(delay)
                    continue
                if self.paper_mode:
                    logger.info("PAPER FILL (broker error) | %s %s", signal.symbol, signal.side)
                    result = await self._simulate_fill(order, signal, broker=broker)
                    await self._persist_result(session_factory, order, result, signal)
                    return result
                self._maybe_auto_kill("place_order failure", broker=str(signal.broker or "").strip().lower())
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
        await self._persist_result(session_factory, order, result, signal)
        return result

    def _paper_fee_bps(self) -> Decimal:
        risk_engine = get_risk_engine()
        cfg = getattr(risk_engine, "config", {}) if risk_engine is not None else {}
        try:
            return Decimal(str(cfg.get("paper_fee_bps", 10)))
        except Exception:  # noqa: BLE001
            return Decimal("10")

    def _paper_slippage_bps(self) -> Decimal:
        """Return one-way slippage applied in the trade direction (default 2 bps)."""
        risk_engine = get_risk_engine()
        cfg = getattr(risk_engine, "config", {}) if risk_engine is not None else {}
        try:
            return Decimal(str(cfg.get("paper_slippage_bps", 2)))
        except Exception:  # noqa: BLE001
            return Decimal("2")

    def _paper_partial_fill_rate(self) -> float:
        """
        Probability [0.0, 1.0] that a paper fill is partial rather than full.
        Default 0.0 (deterministic full fill) — set ``paper_partial_fill_rate``
        in risk_limits.yaml to simulate realistic partial fills in paper testing.
        """
        risk_engine = get_risk_engine()
        cfg = getattr(risk_engine, "config", {}) if risk_engine is not None else {}
        try:
            return float(cfg.get("paper_partial_fill_rate", 0.0))
        except Exception:  # noqa: BLE001
            return 0.0

    async def _simulate_fill(
        self,
        order: Order,
        signal: Signal,
        broker: Any | None = None,
    ) -> OrderResult:
        """
        Create a synthetic filled order for paper mode.

        Modelling features:
        - Fee: applies ``paper_fee_bps`` on filled notional (default 10 bps).
        - Slippage: applies ``paper_slippage_bps`` in trade direction — BUY fills
          slightly above mid, SELL slightly below (default 2 bps).
        - Limit bounds: LIMIT orders never fill worse than their limit price.
        - Partial fills: when ``paper_partial_fill_rate`` > 0, fills a random
          fraction (50-95%) of the order with probability equal to the rate.
        """
        import random

        fee_bps = self._paper_fee_bps()
        slippage_bps = self._paper_slippage_bps()
        partial_fill_rate = self._paper_partial_fill_rate()

        # ── 1. Determine base fill price ──────────────────────────────────────
        fill_price: Decimal | None = None
        if signal.suggested_price is not None and signal.suggested_price > 0:
            fill_price = signal.suggested_price
        elif order.limit_price is not None and order.limit_price > 0:
            fill_price = order.limit_price

        if fill_price is None or fill_price <= 0:
            if broker is not None:
                try:
                    fill_price = await broker.get_last_price(order.symbol)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "Paper fill: get_last_price failed | symbol=%s | %s",
                        order.symbol,
                        exc,
                    )
                    fill_price = Decimal("0")
            else:
                fill_price = Decimal("0")

        # ── 2. Apply directional slippage ─────────────────────────────────────
        if fill_price is not None and fill_price > 0 and slippage_bps > 0:
            slip_factor = slippage_bps / Decimal("10000")
            if order.side == OrderSide.BUY:
                fill_price = fill_price * (Decimal("1") + slip_factor)
            else:
                fill_price = fill_price * (Decimal("1") - slip_factor)

        # ── 3. Enforce limit price bounds ─────────────────────────────────────
        if order.order_type == OrderType.LIMIT and order.limit_price is not None and order.limit_price > 0:
            lp = order.limit_price
            if fill_price is None or fill_price <= 0:
                fill_price = lp
            elif order.side == OrderSide.BUY:
                # BUY limit: must not fill above the limit price
                fill_price = min(fill_price, lp)
            else:
                # SELL limit: must not fill below the limit price
                fill_price = max(fill_price, lp)

        # ── 4. Simulate partial fill ──────────────────────────────────────────
        filled_qty = order.quantity
        status = OrderStatus.FILLED
        if partial_fill_rate > 0.0 and random.random() < partial_fill_rate:
            # Fill between 50% and 95% of the requested quantity
            ratio = Decimal(str(round(0.50 + random.random() * 0.45, 6)))
            filled_qty = (order.quantity * ratio).quantize(Decimal("0.00000001"))
            status = OrderStatus.PARTIALLY_FILLED

        # ── 5. Compute fee on filled notional ─────────────────────────────────
        notional = abs(filled_qty * fill_price) if (fill_price is not None and fill_price > 0) else Decimal("0")
        fee = (notional * fee_bps / Decimal("10000")).quantize(Decimal("0.00000001"))
        avg = fill_price if (fill_price is not None and fill_price > 0) else None

        return OrderResult(
            broker_order_id=f"paper-{uuid.uuid4().hex[:12]}",
            client_order_id=order.client_order_id,
            status=status,
            symbol=order.symbol,
            side=order.side,
            quantity=order.quantity,
            filled_quantity=filled_qty,
            avg_fill_price=avg,
            fee=fee,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

    async def _execute_arbitrage(
        self,
        signal: Signal,
        *,
        session_factory=None,
    ) -> Optional[OrderResult]:
        """Paired-leg routing: funding (spot+perp) or cross-spot; paper mode simulates a single audit leg."""
        md = signal.metadata if isinstance(signal.metadata, dict) else {}
        side_u = (signal.side or "").strip().upper()
        qty = signal.suggested_quantity

        logger.info(
            "ARBITRAGE | signal_id=%s | %s | %s | qty=%s | paper=%s",
            signal.signal_id,
            signal.symbol,
            side_u,
            qty,
            self.paper_mode,
        )

        paper_order = Order(
            symbol=signal.symbol,
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            quantity=qty,
            client_order_id=str(uuid.uuid4()),
        )

        if self.paper_mode:
            arb_broker = await self._get_broker(signal.broker)
            result = await self._simulate_fill(paper_order, signal, broker=arb_broker)
            await self._persist_result(session_factory, paper_order, result, signal)
            logger.info(
                "ARBITRAGE PAPER | audit fill on primary broker=%s | paired venues in metadata",
                signal.broker,
            )
            return result

        if "SPOT_SPREAD" in side_u:
            buy_v = str(md.get("buy_venue", "")).strip().lower()
            sell_v = str(md.get("sell_venue", "")).strip().lower()
            brokers: dict[str, Any] = {}
            for n in (buy_v, sell_v):
                if n and n not in brokers:
                    b = await self._get_broker(n)
                    if b is not None:
                        brokers[n] = b
            ex = SpotArbitrageExecutor(brokers, logger)
            sig_d = {
                "symbol": signal.symbol,
                "buy_venue": buy_v,
                "sell_venue": sell_v,
                "metadata": md,
            }
            await ex.execute(sig_d, qty)
            return OrderResult(
                broker_order_id=f"arb-spot-{uuid.uuid4().hex[:12]}",
                client_order_id=paper_order.client_order_id,
                status=OrderStatus.FILLED,
                symbol=signal.symbol,
                side=OrderSide.BUY,
                quantity=qty,
                filled_quantity=qty,
                avg_fill_price=signal.suggested_price,
                fee=Decimal("0"),
                timestamp=datetime.now(timezone.utc).isoformat(),
            )

        spot_v = str(md.get("spot_venue", signal.broker)).strip().lower()
        perp_v = str(md.get("perp_venue", "")).strip().lower()
        brokers2: dict[str, Any] = {}
        for n in (spot_v, perp_v):
            if n and n not in brokers2:
                b = await self._get_broker(n)
                if b is not None:
                    brokers2[n] = b
        rk = get_risk_engine()
        acfg = (getattr(rk, "config", {}) or {}).get("arbitrage") if rk is not None else {}
        flatten = bool((acfg or {}).get("flatten_on_leg_failure", True))
        arb = ArbitrageExecutor(brokers2, logger, flatten_on_failure=flatten)
        await arb.open_pair(signal, qty)
        return OrderResult(
            broker_order_id=f"arb-fund-{uuid.uuid4().hex[:12]}",
            client_order_id=paper_order.client_order_id,
            status=OrderStatus.FILLED,
            symbol=signal.symbol,
            side=OrderSide.BUY,
            quantity=qty,
            filled_quantity=qty,
            avg_fill_price=signal.suggested_price,
            fee=Decimal("0"),
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

    async def _persist_result(
        self, session_factory, order: Order, result: OrderResult, signal: Signal
    ) -> None:
        # Telegram notification for fills is emitted regardless of persistence,
        # so a missing session_factory (unit test / degraded mode) still surfaces
        # the trade to the operator.
        await self._maybe_notify_fill(order, result, signal)
        if session_factory is None:
            return
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

    async def _maybe_notify_fill(
        self, order: Order, result: OrderResult, signal: Signal
    ) -> None:
        """Emit a single Telegram notification per persisted fill.

        Operator preference (post-D031): only open/close position events reach
        Telegram — no per-signal or per-reject chatter. A FILLED or
        PARTIALLY_FILLED OrderResult is the authoritative trigger because every
        fill either opens, adds to, reduces, or closes a position.
        """
        try:
            status = getattr(result, "status", None)
            if status not in (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED):
                return
            filled_qty = Decimal(str(getattr(result, "filled_quantity", 0) or 0))
            if filled_qty <= 0:
                return
            price = getattr(result, "avg_fill_price", None)
            price_dec = Decimal(str(price)) if price is not None else Decimal("0")
            notional = (filled_qty * price_dec).quantize(Decimal("0.01")) if price_dec > 0 else None

            if signal.side:
                side = str(signal.side).upper()
            elif hasattr(order.side, "value"):
                side = str(order.side.value).upper()
            else:
                side = str(order.side).upper()
            sig_md = signal.metadata if isinstance(getattr(signal, "metadata", None), dict) else {}
            reduce_only = bool(
                getattr(order, "reduce_only", False)
                or getattr(signal, "reduce_only", False)
                or sig_md.get("reduce_only")
                or str(sig_md.get("coordinator_kind", "")).lower().startswith("close")
            )
            action_label = "CLOSE" if reduce_only else "OPEN"
            status_label = "FILLED" if status == OrderStatus.FILLED else "PARTIAL"
            mode_label = "PAPER" if self.paper_mode else "LIVE"

            parts = [
                f"{mode_label} {action_label} {status_label}",
                f"{signal.symbol} {side}",
                f"qty={filled_qty.normalize() if filled_qty == filled_qty.to_integral() else filled_qty}",
            ]
            if price_dec > 0:
                parts.append(f"@ {price_dec}")
            if notional is not None:
                parts.append(f"notional={notional}")
            parts.append(f"broker={signal.broker}")
            message = " | ".join(parts)
            await self._send_critical_alert(message)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Fill Telegram notification failed | signal_id=%s | %s",
                getattr(signal, "signal_id", "?"),
                exc,
            )

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

    async def reconcile_positions(
        self,
        *,
        session_factory=None,
        max_quantity_diff: Decimal = Decimal("0.000001"),
    ) -> bool:
        """
        Compare broker-reported positions against latest local snapshot.
        Returns True when consistent; False when mismatch/failure.
        """
        try:
            ok = await self._reconcile_positions_internal(
                session_factory=session_factory,
                max_quantity_diff=max_quantity_diff,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("Position reconciliation failed | %s", exc)
            self._maybe_auto_kill_reconciliation("reconciliation exception", broker=None)
            return False
        return ok

    async def _find_in_flight_order(
        self,
        session_factory,
        signal: Signal,
    ) -> Optional["OrderLog"]:  # type: ignore[name-defined]
        """Return an existing non-terminal order for this (symbol, side, broker).

        Uses the ``orders`` table because a fresh process restart has an
        empty in-memory ``_open_orders`` dict — we must not re-emit orders
        that the previous runner already parked in the broker book.

        Matches on status in {``pending``, ``open``, ``partially_filled``}
        and timestamp within ``dedup_window_sec`` of ``now``.
        """
        if session_factory is None:
            return None
        sym = (signal.symbol or "").strip().upper()
        side = (signal.side or "").strip().lower()
        broker = (signal.broker or "").strip().lower()
        if not sym or not broker or side not in ("buy", "sell"):
            return None
        try:
            from sqlalchemy import select  # local import to keep module import light
            from storage.models import OrderLog

            cutoff = datetime.now(timezone.utc) - timedelta(seconds=self.dedup_window_sec)
            async with session_factory() as session:
                stmt = (
                    select(OrderLog)
                    .where(
                        OrderLog.symbol == sym,
                        OrderLog.broker == broker,
                        OrderLog.side == side,
                        OrderLog.status.in_(("pending", "open", "partially_filled")),
                        OrderLog.timestamp >= cutoff,
                    )
                    .order_by(OrderLog.timestamp.desc())
                    .limit(1)
                )
                q = await session.execute(stmt)
                return q.scalars().first()
        except Exception as exc:  # noqa: BLE001
            # Dedup is best-effort — on DB failure we fall through and let
            # the order place. Safer than blocking trading on a DB hiccup.
            logger.warning("Order dedup lookup failed (%s); allowing order", exc)
            return None

    def _build_order(self, signal: Signal) -> Order:
        meta = signal.metadata if isinstance(getattr(signal, "metadata", None), dict) else {}
        inst_meta = None
        if isinstance(meta.get("option_contract"), dict):
            inst_meta = {
                "instrument_type": "option",
                "option_contract": dict(meta["option_contract"]),
            }
        spec = parse_option_contract_from_metadata(meta)
        sym = spec.position_key() if spec is not None else signal.symbol
        return Order(
            symbol=sym,
            side=OrderSide.BUY if signal.side == "buy" else OrderSide.SELL,
            order_type=OrderType.MARKET if signal.suggested_price is None else OrderType.LIMIT,
            quantity=signal.suggested_quantity,
            limit_price=signal.suggested_price,
            client_order_id=str(uuid.uuid4()),  # idempotency key
            instrument_metadata=inst_meta,
        )

    async def _apply_marketable_limit(
        self,
        order: Order,
        signal: Signal,
        broker: Any,
    ) -> Order:
        """Rewrite the limit price into a *marketable* one using live top-of-book.

        The allocator emits ``suggested_price = last 1h-bar close`` which, after
        even a tiny upward drift, becomes an unmarketable bid that sits in the
        broker queue forever. By fetching live bid/ask at placement time and
        pricing BUYs at ``ask × (1 + slip)`` / SELLs at ``bid × (1 - slip)``
        we produce orders that cross the spread and fill immediately.

        Fallback chain when quotes are unavailable:
          1. Order book top-of-book (primary)
          2. ``broker.get_last_price`` ± slip (secondary)
          3. Keep the original ``suggested_price`` (legacy behaviour)

        Skipped entirely when ``self.marketable_slip_bps <= 0``.
        """
        if self.marketable_slip_bps <= 0:
            return order
        if order.order_type != OrderType.LIMIT:
            return order
        if broker is None:
            return order

        slip_factor = Decimal(str(self.marketable_slip_bps)) / Decimal("10000")
        side_is_buy = order.side == OrderSide.BUY

        bid: Decimal | None = None
        ask: Decimal | None = None
        try:
            ob: OrderBook = await broker.get_order_book(order.symbol, depth=1)
            if ob is not None:
                if ob.bids:
                    b0 = ob.bids[0][0]
                    if b0 is not None and b0 > 0:
                        bid = Decimal(str(b0))
                if ob.asks:
                    a0 = ob.asks[0][0]
                    if a0 is not None and a0 > 0:
                        ask = Decimal(str(a0))
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "Marketable limit: order book fetch failed | symbol=%s | %s",
                order.symbol,
                exc,
            )

        reference: Decimal | None = None
        source = "book"
        if side_is_buy and ask is not None and ask > 0:
            reference = ask
        elif (not side_is_buy) and bid is not None and bid > 0:
            reference = bid

        if reference is None:
            # Fall back to last traded price when the book is empty or one-sided.
            try:
                last_px = await broker.get_last_price(order.symbol)
                if last_px is not None and last_px > 0:
                    reference = Decimal(str(last_px))
                    source = "last"
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "Marketable limit: get_last_price failed | symbol=%s | %s",
                    order.symbol,
                    exc,
                )

        if reference is None or reference <= 0:
            return order  # keep original — no fresh reference available

        if side_is_buy:
            new_px = reference * (Decimal("1") + slip_factor)
        else:
            new_px = reference * (Decimal("1") - slip_factor)

        # Venue-specific tick rounding is handled inside each adapter's
        # ``place_order`` (e.g. Alpaca quantizes sub-penny prices). We pass the
        # raw Decimal so every adapter can apply its own rules consistently.
        if new_px <= 0:
            return order

        self.marketable_adjusted += 1
        logger.info(
            "MARKETABLE LIMIT | %s %s reference=%s (%s) slip_bps=%s old=%s new=%s",
            signal.symbol,
            signal.side,
            reference,
            source,
            self.marketable_slip_bps,
            order.limit_price,
            new_px,
        )
        return replace(order, limit_price=new_px)

    def _normalize_order_for_broker(self, order: Order, signal: Signal) -> Order:
        """
        Apply venue-specific quantity constraints before placement.

        IBKR rejects fractional quantity for many non-crypto instruments (error 10243).
        Normalize those to whole units to avoid immediate cancel.
        """
        broker = str(getattr(signal, "broker", "") or "").strip().lower()
        asset = str(getattr(signal, "asset_class", "") or "").strip().lower()
        qty = Decimal(str(order.quantity))

        if broker == "ibkr" and asset in {"equity", "etf", "bond", "future", "option"}:
            qty = qty.quantize(Decimal("1"), rounding=ROUND_DOWN)

        if qty == order.quantity:
            return order
        return replace(order, quantity=qty)

    async def _get_broker(self, name: str):
        """Lazy-load broker adapter."""
        key = (name or "").strip().lower()
        if not key:
            return None
        if key not in self._brokers:
            bm = getattr(self, "_broker_manager", None)
            adapters = getattr(bm, "adapters", None) if bm is not None else None
            if isinstance(adapters, dict) and key in adapters:
                broker = adapters[key]
                try:
                    connected = await broker.is_connected()
                except Exception:  # noqa: BLE001
                    connected = False
                if not connected:
                    try:
                        connected = bool(await broker.connect())
                    except Exception as exc:  # noqa: BLE001
                        logger.error("Broker connect raised | broker=%s | %s", key, exc)
                        return None
                    if not connected:
                        logger.error("Broker connect failed | broker=%s", key)
                        return None
                self._brokers[key] = broker
                return broker
            config = self.broker_configs.get(key, self.broker_configs.get(name, {}))
            broker = get_broker(
                key,
                paper_mode=self.paper_mode,
                **config
            )
            try:
                connected = await broker.connect()
            except Exception as exc:  # noqa: BLE001
                logger.error("Broker connect raised | broker=%s | %s", key, exc)
                return None
            if not connected:
                logger.error("Broker connect failed | broker=%s", key)
                return None
            self._brokers[key] = broker
        return self._brokers[key]

    async def _reconnect_broker(self, name: str) -> bool:
        key = (name or "").strip().lower()
        broker = self._brokers.get(key)
        if broker is None:
            broker = await self._get_broker(key)
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

    def _passes_sizing_boundary_guard(self, order: Order, signal: Signal) -> bool:
        """D031C — reject orders whose notional materially exceeds the coordinator's intent.

        The global-edge coordinator attaches ``sizing_final_capital_required``
        (and, as a fallback, ``target_notional``) to every directional signal's
        metadata. If the concrete order about to hit the broker deviates by
        more than ``SIZING_BOUNDARY_TOLERANCE`` (default 1.25×) from that
        intended size, *something upstream is broken* — the safe action is to
        refuse to place the order.

        Also rejects when the order exceeds the sizing hard cap
        (``sizing_hard_cap_notional``) regardless of intent, in case the
        strategy requested an absurd size that slipped through.

        Arbitrage legs are exempt (they carry capital via different paths).
        If the signal metadata carries no sizing audit fields at all (legacy
        path or external signal) the guard is a no-op — we never fabricate a
        limit from nothing.
        """
        side_up = (signal.side or "").strip().upper()
        if side_up.startswith("ARBITRAGE_"):
            return True

        md = signal.metadata if isinstance(getattr(signal, "metadata", None), dict) else {}

        def _to_dec(v: Any) -> Optional[Decimal]:
            if v is None:
                return None
            try:
                return Decimal(str(v))
            except Exception:  # noqa: BLE001
                return None

        intended = _to_dec(md.get("sizing_final_capital_required")) or _to_dec(md.get("target_notional"))
        hard_cap = _to_dec(md.get("sizing_hard_cap_notional"))

        px = order.limit_price if (order.limit_price is not None and order.limit_price > 0) else signal.suggested_price
        if px is None or px <= 0:
            return True
        actual_notional = abs(Decimal(str(order.quantity))) * Decimal(str(px))

        tolerance = Decimal("1.25")
        if intended is not None and intended > 0 and actual_notional > intended * tolerance:
            logger.critical(
                "SIZING GUARD REJECT | signal_id=%s symbol=%s side=%s broker=%s | "
                "actual_notional=%s > intended=%s * %s | sizing_source=%s",
                signal.signal_id,
                signal.symbol,
                signal.side,
                signal.broker,
                actual_notional,
                intended,
                tolerance,
                md.get("sizing_source"),
            )
            return False

        if hard_cap is not None and hard_cap > 0 and actual_notional > hard_cap:
            logger.critical(
                "SIZING GUARD REJECT (hard cap) | signal_id=%s symbol=%s | "
                "actual_notional=%s > hard_cap=%s",
                signal.signal_id,
                signal.symbol,
                actual_notional,
                hard_cap,
            )
            return False

        if intended is not None and intended > 0:
            ratio = (actual_notional / intended).quantize(Decimal("0.0001"))
            logger.info(
                "SIZING OK | signal_id=%s %s %s | actual=%s intended=%s ratio=%s source=%s",
                signal.signal_id,
                signal.symbol,
                signal.side,
                actual_notional,
                intended,
                ratio,
                md.get("sizing_source"),
            )
        return True

    async def _passes_execution_limits(self, broker, order: Order, *, broker_name: str) -> bool:
        if self.paper_mode:
            return True

        limits = self._execution_limits()
        try:
            ob: OrderBook = await broker.get_order_book(order.symbol, depth=10)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Order book fetch failed | symbol=%s | %s", order.symbol, exc)
            self._maybe_auto_kill("order book fetch failure", broker=broker_name or None)
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

    async def _publish_symbol_constraints(self, signal: Signal, broker) -> None:
        """
        Best-effort runtime symbol minimum notional inference.
        Uses adapter internals where available, without changing the frozen broker interface.
        """
        risk_engine = get_risk_engine()
        if risk_engine is None or not hasattr(risk_engine, "set_live_parameter"):
            return
        asset = (signal.asset_class or "").strip().lower()
        symbol = (signal.symbol or "").strip().upper()
        inferred = await self._infer_min_order_notional(symbol, asset, broker)
        if inferred is None or inferred <= 0:
            return
        try:
            risk_engine.set_live_parameter(f"minimum_order_size.symbol.{symbol}", inferred)
            risk_engine.set_live_parameter(f"minimum_order_size.asset_class.{asset}", inferred)
            logger.debug(
                "Published live minimum order | symbol=%s asset=%s min_notional=%s",
                symbol,
                asset,
                inferred,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to publish live minimum order | symbol=%s | %s", symbol, exc)

    async def _infer_min_order_notional(self, symbol: str, asset_class: str, broker) -> Optional[Decimal]:
        name = getattr(broker, "broker_name", "").strip().lower()
        try:
            if name == "binance":
                return await self._infer_binance_min_notional(symbol, broker)
            if name == "kraken":
                return await self._infer_kraken_min_notional(symbol, broker)
            if name == "alpaca":
                return await self._infer_alpaca_min_notional(symbol, broker)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Symbol minimum inference failed | broker=%s symbol=%s | %s", name, symbol, exc)
        return None

    async def _infer_binance_min_notional(self, symbol: str, broker) -> Optional[Decimal]:
        client = getattr(broker, "_client", None)
        if client is None:
            return None
        sym = symbol.replace("/", "").upper()
        info = await asyncio.to_thread(lambda: client.get_symbol_info(sym))
        if not isinstance(info, dict):
            return None
        filters = info.get("filters", [])
        for f in filters:
            if not isinstance(f, dict):
                continue
            t = str(f.get("filterType", "")).upper()
            if t in {"NOTIONAL", "MIN_NOTIONAL"}:
                val = f.get("minNotional") or f.get("notional")
                if val:
                    return Decimal(str(val))
        return None

    async def _infer_kraken_min_notional(self, symbol: str, broker) -> Optional[Decimal]:
        market = getattr(broker, "_market", None)
        if market is None:
            return None
        pair = symbol.replace("BTC/", "XBT").replace("/", "")
        data = await asyncio.to_thread(lambda: market.get_asset_pairs(pair=pair))
        if not isinstance(data, dict) or not data:
            return None
        row = next(iter(data.values()))
        if not isinstance(row, dict):
            return None
        ordemin = row.get("ordermin")
        if not ordemin:
            return None
        qty = Decimal(str(ordemin))
        px = await broker.get_last_price(symbol)
        if px <= 0:
            return None
        return qty * px

    async def _infer_alpaca_min_notional(self, symbol: str, broker) -> Optional[Decimal]:
        trading = getattr(broker, "_trading", None)
        if trading is None:
            return None
        asset = await asyncio.to_thread(lambda: trading.get_asset(symbol))
        min_order = getattr(asset, "min_order_size", None)
        if min_order is None:
            # Alpaca often has no hard per-symbol minimum on equities; leave fallback in place.
            return None
        qty = Decimal(str(min_order))
        px = await broker.get_last_price(symbol)
        if px <= 0:
            return None
        return qty * px

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

    def _maybe_auto_kill(self, reason: str, *, broker: str | None = None) -> None:
        limits = self._execution_limits()
        if not limits["auto_kill_on_api_failure"]:
            return
        risk_engine = get_risk_engine()
        if risk_engine is None:
            return
        use_global = os.getenv("EXECUTION_AUTO_KILL_GLOBAL", "").strip().lower() in ("1", "true", "yes")
        try:
            if use_global:
                risk_engine.kill()
                logger.critical("Auto-kill (global) triggered by execution failure: %s", reason)
            elif broker:
                risk_engine.disable_broker(broker)
                logger.critical("Auto-disable broker triggered by execution failure: %s | broker=%s", reason, broker)
            else:
                risk_engine.kill()
                logger.critical("Auto-kill (global) triggered by execution failure: %s", reason)
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to auto-kill/disable on execution failure: %s", exc)

    async def _reconcile_positions_internal(self, *, session_factory=None, max_quantity_diff: Decimal) -> bool:
        from sqlalchemy import func, select
        from storage.models import PositionLog

        own_engine = None
        sf = session_factory
        if sf is None:
            own_engine, sf = await self._init_db()
        if sf is None:
            logger.warning("Position reconciliation skipped | DB unavailable")
            return True
        try:
            local: dict[tuple[str, str], Decimal] = {}
            async with sf() as session:
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

            # Ensure we attempt broker reconciliation even before any order execution.
            preload_names = self.allowed_brokers if self.allowed_brokers else list(self.broker_configs.keys())
            for broker_name in preload_names:
                if broker_name in self._brokers:
                    continue
                if not self._broker_seems_configured(broker_name):
                    continue
                await self._get_broker(broker_name)

            remote: dict[tuple[str, str], Decimal] = {}
            remote_snapshots: list[tuple[str, Position]] = []
            for broker_name, broker in self._brokers.items():
                try:
                    positions: list[Position] = await broker.get_positions()
                except Exception as exc:  # noqa: BLE001
                    logger.error("Broker positions fetch failed | broker=%s | %s", broker_name, exc)
                    self._maybe_auto_kill_reconciliation("reconciliation exception", broker=broker_name.strip().lower())
                    return False
                for p in positions:
                    key = (broker_name.strip().lower(), str(p.symbol).strip().upper())
                    remote[key] = remote.get(key, Decimal("0")) + Decimal(str(p.quantity))
                    remote_snapshots.append((broker_name, p))

            # Compare first, then ALWAYS persist the remote snapshot. The
            # broker is the authoritative source of truth for what we own; if
            # the DB diverges, the correct response is to refresh the DB so
            # downstream consumers (allocator's ``held`` input, UI positions,
            # risk sizing) see reality — not to silently preserve the stale
            # local view, which is what the pre-D030 early-return did.
            keys = set(local.keys()) | set(remote.keys())
            any_mismatch = False
            first_mismatch_broker: str | None = None
            for key in keys:
                lq = local.get(key, Decimal("0"))
                rq = remote.get(key, Decimal("0"))
                if abs(lq - rq) > max_quantity_diff:
                    any_mismatch = True
                    if first_mismatch_broker is None:
                        first_mismatch_broker = key[0]
                    logger.error(
                        "Position mismatch | broker=%s symbol=%s local_qty=%s remote_qty=%s",
                        key[0],
                        key[1],
                        lq,
                        rq,
                    )

            # Persist latest remote broker positions as a fresh snapshot so
            # API/UI/allocator see real holdings, mismatch or not.
            if remote_snapshots:
                snap_ts = datetime.now(timezone.utc)
                async with sf() as session:
                    for broker_name, p in remote_snapshots:
                        im = getattr(p, "instrument_metadata", None)
                        session.add(
                            PositionLog(
                                timestamp=snap_ts,
                                symbol=str(p.symbol).strip().upper()[:72],
                                broker=str(broker_name).strip().lower()[:20],
                                quantity=Decimal(str(p.quantity)),
                                avg_entry_price=Decimal(str(p.avg_entry_price)),
                                current_price=Decimal(str(p.current_price)),
                                unrealised_pnl=Decimal(str(p.unrealised_pnl)),
                                asset_class=str(p.asset_class.value if hasattr(p.asset_class, "value") else p.asset_class)
                                .strip()
                                .lower()[:20],
                                instrument_metadata=im if isinstance(im, dict) else None,
                            )
                        )
                    await session.commit()

            if any_mismatch:
                # Still run the optional auto-kill hook — operators that opt in
                # via ``auto_kill_on_reconciliation_failure`` get the same
                # protection as before.
                self._maybe_auto_kill_reconciliation(
                    "position mismatch", broker=first_mismatch_broker
                )
                return False
            return True
        finally:
            if own_engine is not None:
                await self._dispose_db(own_engine)

    def _broker_seems_configured(self, name: str) -> bool:
        cfg = self.broker_configs.get(name, {}) or {}
        name = (name or "").strip().lower()
        if name == "ibkr":
            # IBKR host/port/client_id defaults are acceptable; connectivity checked in connect().
            return True
        if name in {"kraken", "binance", "bybit", "alpaca"}:
            return bool(str(cfg.get("api_key", "")).strip() and str(cfg.get("api_secret", "")).strip())
        return True

    def _maybe_auto_kill_reconciliation(self, reason: str, *, broker: str | None) -> None:
        limits = self._execution_limits()
        if not limits["auto_kill_on_reconciliation_failure"]:
            return
        risk_engine = get_risk_engine()
        if risk_engine is None:
            return
        use_global = os.getenv("EXECUTION_AUTO_KILL_GLOBAL", "").strip().lower() in ("1", "true", "yes")
        try:
            if use_global:
                risk_engine.kill()
                logger.critical("Auto-kill (global) triggered by reconciliation failure: %s", reason)
            elif broker:
                risk_engine.disable_broker(broker)
                logger.critical("Auto-disable broker triggered by reconciliation failure: %s | broker=%s", reason, broker)
            else:
                risk_engine.kill()
                logger.critical("Auto-kill (global) triggered by reconciliation failure: %s", reason)
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to auto-kill/disable on reconciliation failure: %s", exc)

    @staticmethod
    async def _init_db():
        from storage.db import init_async_database

        return await init_async_database()

    @staticmethod
    async def _dispose_db(engine) -> None:
        from storage.db import dispose_engine

        await dispose_engine(engine)

    async def _send_critical_alert(self, message: str) -> None:
        # Never send real Telegram alerts from unit tests.
        if os.getenv("PYTEST_CURRENT_TEST"):
            return
        disable = (os.getenv("MYTBOT_DISABLE_TELEGRAM_ALERTS", "") or "").strip().lower()
        if disable in ("1", "true", "yes", "on"):
            return
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
