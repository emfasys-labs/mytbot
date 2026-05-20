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
from brokers.base import AssetClass, Order, OrderBook, OrderResult, OrderSide, OrderStatus, OrderType, Position
from core.broker_paper import NO_NATIVE_PAPER_POSITION_BROKERS
from core.instruments import parse_option_contract_from_metadata
from risk.engine import Signal, RiskDecision, RiskVerdict

from execution.arbitrage_executor import ArbitrageExecutor
from execution.arbitrage_spot_executor import SpotArbitrageExecutor
from execution.microstructure_shadow import build_microstructure_shadow_metadata

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
        self.last_skip_reason: str | None = None
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
        # Per-broker "balance exhausted" cooldown timestamps (set when a
        # broker rejects with an insufficient-balance message; cleared by
        # the next successful fill on that broker).
        self._broker_balance_exhausted_until: dict[str, datetime] = {}
        try:
            self._broker_balance_cooldown_sec = float(
                os.getenv("EXECUTION_BROKER_BALANCE_COOLDOWN_SEC", "1800") or "1800"
            )
        except (TypeError, ValueError):
            self._broker_balance_cooldown_sec = 1800.0
        # Wave 9 — cost-aware pre-flight gate. Auto-loaded once at construction
        # so each `execute()` doesn't re-parse YAML. Disabled by default.
        try:
            from execution.wave9_runtime import Wave9RuntimeConfig

            self._wave9_cfg = Wave9RuntimeConfig.load()
        except Exception as exc:  # noqa: BLE001
            logger.warning("execution_engine | wave9 config load failed: %s", exc)
            self._wave9_cfg = None
        # Counters for ops visibility on the Wave 13 dashboard.
        self.wave9_gate_blocked = 0
        self.wave9_gate_passed = 0
        set_execution_engine(self)

    def reload_wave9_config(self) -> None:
        """Test/operator helper — re-read config/execution_models.yaml."""
        try:
            from execution.wave9_runtime import Wave9RuntimeConfig

            self._wave9_cfg = Wave9RuntimeConfig.load()
        except Exception as exc:  # noqa: BLE001
            logger.warning("execution_engine | wave9 reload failed: %s", exc)
            self._wave9_cfg = None

    def _is_broker_balance_exhausted(self, broker_name: str) -> bool:
        until = self._broker_balance_exhausted_until.get(broker_name)
        if until is None:
            return False
        if datetime.now(timezone.utc) >= until:
            self._broker_balance_exhausted_until.pop(broker_name, None)
            return False
        return True

    def _mark_broker_balance_exhausted(self, broker_name: str, reason: str) -> None:
        if not broker_name:
            return
        until = datetime.now(timezone.utc) + timedelta(
            seconds=max(60.0, self._broker_balance_cooldown_sec)
        )
        self._broker_balance_exhausted_until[broker_name] = until
        logger.warning(
            "BROKER BALANCE EXHAUSTED | broker=%s | cooldown_until=%s | reason=%s",
            broker_name, until.isoformat(), reason,
        )

    def _clear_broker_balance_exhausted(self, broker_name: str) -> None:
        if broker_name in self._broker_balance_exhausted_until:
            self._broker_balance_exhausted_until.pop(broker_name, None)

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
        self.last_skip_reason = None

        if risk_decision.verdict != RiskVerdict.APPROVED:
            logger.warning(f"Attempted to execute rejected signal {signal.signal_id}")
            self.last_skip_reason = "risk_not_approved"
            return None

        if (signal.side or "").strip().upper().startswith("ARBITRAGE_"):
            return await self._execute_arbitrage(signal, session_factory=session_factory)

        # ── Market-session validity gate ─────────────────────────────────
        # A venue physically cannot fill a closed market. In paper mode the
        # simulator was otherwise filling equity/forex orders on weekends
        # and overnight against stale prices — manufacturing fake churn and
        # polluting every P&L/evidence number. A real broker could not have
        # filled these either, so refusing them is correctness, not a cap.
        # Crypto (24/7) and unclassifiable instruments are never blocked.
        try:
            from core.market_session import is_tradeable, not_tradeable_reason

            if not is_tradeable(
                signal.broker, signal.asset_class, str(signal.symbol or "")
            ):
                reason = not_tradeable_reason(
                    signal.broker, signal.asset_class, str(signal.symbol or "")
                ) or "market_closed"
                self.last_skip_reason = reason
                logger.info(
                    "MARKET CLOSED — skipping %s %s broker=%s (%s)",
                    signal.symbol,
                    signal.side,
                    signal.broker,
                    reason,
                )
                return None
        except Exception as exc:  # noqa: BLE001 — gate must never crash exec
            logger.debug("market-session gate skipped (non-fatal): %s", exc)

        # Dedup: if a non-terminal order for (symbol, side, broker) already
        # exists within ``dedup_window_sec``, skip emitting a duplicate. This
        # prevents the allocator from re-submitting the same opportunity each
        # loop iteration when a prior limit order is still sitting unfilled.
        sig_md = signal.metadata if isinstance(getattr(signal, "metadata", None), dict) else {}
        if self.dedup_window_sec > 0:
            existing = await self._find_in_flight_order(session_factory, signal)
            if existing is not None:
                if bool(sig_md.get("flatten_all")):
                    if not await self._cancel_in_flight_order(session_factory, existing, signal):
                        self.dedup_skipped += 1
                        logger.warning(
                            "FLATTEN REPLACE BLOCKED | %s %s broker=%s existing=%s status=%s",
                            signal.symbol,
                            signal.side,
                            signal.broker,
                            existing.id,
                            existing.status,
                        )
                        self.last_skip_reason = "dedup_flatten_replace_blocked"
                        return None
                else:
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
                    self.last_skip_reason = "dedup_existing_in_flight_order"
                    return None

        # Wave 9 — pre-flight cost-aware gate. When enabled, computes the
        # all-in expected cost (impact + fee + spread + slippage prior),
        # consults the urgency policy, and short-circuits with a logged
        # "DO_NOT_TRADE" if cost dwarfs the edge or exceeds the operator's
        # ceiling. Disabled by default — module returns ``allow=True,
        # used=False`` and the engine proceeds unmodified.
        wave9_metadata: dict = {}
        if self._wave9_cfg is not None and getattr(self._wave9_cfg, "enabled", False):
            from execution.wave9_runtime import pre_flight_cost_gate
            from brokers.base import AssetClass as _AssetClass

            try:
                ac_str = (
                    signal.asset_class.value
                    if isinstance(signal.asset_class, _AssetClass)
                    else str(signal.asset_class or "other")
                )
            except Exception:  # noqa: BLE001
                ac_str = "other"
            try:
                qty_f = float(signal.suggested_quantity or 0)
            except (TypeError, ValueError):
                qty_f = 0.0
            gate = pre_flight_cost_gate(
                config=self._wave9_cfg,
                broker=str(signal.broker or ""),
                symbol=str(signal.symbol or ""),
                asset_class=ac_str,
                quantity=qty_f,
                signal_metadata=signal.metadata or {},
            )
            if gate.used:
                wave9_metadata = dict(gate.metadata or {})
            if gate.used and not gate.allow:
                self.wave9_gate_blocked += 1
                logger.info(
                    "WAVE9 GATE BLOCKED | %s %s broker=%s reason=%s cost=%.2fbps",
                    signal.symbol,
                    signal.side,
                    signal.broker,
                    gate.reason,
                    gate.expected_cost_bps,
                )
                self.last_skip_reason = f"wave9_gate:{gate.reason}"
                return None
            if gate.used and gate.allow:
                self.wave9_gate_passed += 1

        order = self._build_order(signal)
        if wave9_metadata:
            # Stamp diagnostic metadata on the order so the dashboard funnel
            # (Wave 13) can render expected vs realised cost. Never overrides
            # caller-provided metadata; only fills missing keys.
            md = dict(order.instrument_metadata or {}) if hasattr(order, "instrument_metadata") else {}
            for k, v in wave9_metadata.items():
                md.setdefault(k, v)
            try:
                order.instrument_metadata = md
            except Exception:  # noqa: BLE001
                pass

        async def _stamp_microstructure_shadow(_broker) -> None:
            md = dict(getattr(order, "instrument_metadata", None) or {})
            if "microstructure_shadow_used" in md:
                return
            try:
                ac_val = getattr(signal.asset_class, "value", signal.asset_class)
                shadow = await build_microstructure_shadow_metadata(
                    broker=_broker,
                    symbol=str(order.symbol or signal.symbol or ""),
                    asset_class=str(ac_val or "other"),
                )
            except Exception as exc:  # noqa: BLE001
                shadow = {
                    "microstructure_shadow_used": False,
                    "microstructure_shadow_reason": "shadow_exception",
                    "microstructure_shadow_error": str(exc)[:160],
                }
            md.update({k: v for k, v in shadow.items() if isinstance(v, (str, int, float, bool))})
            try:
                order.instrument_metadata = md
            except Exception:  # noqa: BLE001
                pass

        # Paper-mode shortcut for venues without native paper-trading support.
        # Kraken/Binance/Bybit adapters either reject paper orders outright or
        # require a sandbox key path the user hasn't configured. Rather than
        # spamming the order book with synthetic rejects (and burning the
        # 7-day dedup window), simulate the fill locally — same path used
        # when the broker is unreachable.
        broker_name_l = (signal.broker or "").strip().lower()
        if self.paper_mode and broker_name_l in {"kraken", "binance", "bybit"}:
            broker_for_quote = await self._get_broker(signal.broker)
            await _stamp_microstructure_shadow(broker_for_quote)
            # ── Per-venue capital bound ───────────────────────────────────
            # The synthetic wallet is real (paper) capital; a crypto venue
            # may not OPEN beyond its own equity-derived deploy room. Closes
            # / reduce-only ALWAYS pass (they free room and must never be
            # blocked — risk/stop-loss exits included). This is the hard,
            # dynamic stop on the Kraken-style bleed the realised-only
            # governor was structurally blind to.
            _cap_md = signal.metadata if isinstance(getattr(signal, "metadata", None), dict) else {}
            _is_close = bool(
                getattr(order, "reduce_only", False)
                or getattr(signal, "reduce_only", False)
                or _cap_md.get("reduce_only")
                or _cap_md.get("close_only")
                or str(_cap_md.get("coordinator_kind", "")).strip().lower()
                in {"trim_symbol", "close_symbol", "flatten_symbol"}
            )
            if not _is_close:
                try:
                    from system.paper_wallet import venue_deploy_room

                    room = venue_deploy_room(broker_name_l)
                    if room is not None:
                        _px = (
                            signal.suggested_price
                            or order.limit_price
                            or Decimal("0")
                        )
                        _notional = abs(Decimal(str(order.quantity or 0))) * Decimal(str(_px or 0))
                        if _notional > room:
                            self.last_skip_reason = "crypto_venue_capital_exhausted"
                            logger.warning(
                                "EXEC SKIP (venue paper capital) | %s %s broker=%s "
                                "notional=%.2f > room=%.2f — venue wallet bound",
                                signal.symbol, signal.side, broker_name_l,
                                float(_notional), float(room),
                            )
                            return None
                except Exception as exc:  # noqa: BLE001 — bound must never crash exec
                    logger.debug("crypto venue cap check skipped (non-fatal): %s", exc)
            logger.info(
                "PAPER FILL (no native paper on %s) | %s %s qty=%s",
                broker_name_l, signal.symbol, signal.side, signal.suggested_quantity,
            )
            result = await self._simulate_fill(order, signal, broker=broker_for_quote)
            await self._persist_result(session_factory, order, result, signal)
            return result

        # Auto-disable broker if a recent rejection signaled "insufficient
        # balance" — keeps the allocator from looping rejects against an
        # exhausted paper account. Cleared on next successful fill.
        if (
            not (self.paper_mode and not self._use_native_paper_orders())
            and self._is_broker_balance_exhausted(broker_name_l)
        ):
            logger.warning(
                "EXEC SKIP (broker balance exhausted) | %s %s broker=%s",
                signal.symbol, signal.side, signal.broker,
            )
            self.last_skip_reason = "broker_balance_exhausted"
            return None

        broker = await self._get_broker(signal.broker)
        if broker is None:
            # #2b-2 — IBKR resilience. When the routed venue is down (the
            # cause of ``broker is None`` here: disconnect / maintenance
            # window / failed connect) we have NO trustworthy live quote.
            # Simulating a reduce-only *close* would book it at a stale
            # carried/pipeline price and realise fictitious P&L — corrupting
            # exactly the realised curve we judge the soak on, and lying
            # about money. A close you cannot price is a close you defer:
            # skip this cycle and let it execute at a real price once the
            # venue is back (mirrors live reality — you can't close what you
            # can't reach). Opening trades still simulate off the M2
            # pipeline price (independent of IBKR connectivity) as before.
            _sig_md = signal.metadata if isinstance(getattr(signal, "metadata", None), dict) else {}
            _is_reduce_only = bool(
                getattr(order, "reduce_only", False)
                or getattr(signal, "reduce_only", False)
                or _sig_md.get("reduce_only")
                or _sig_md.get("close_only")
                or str(_sig_md.get("coordinator_kind", "")).strip().lower() in {"trim_symbol", "close_symbol", "flatten_symbol"}
                or str(getattr(signal, "strategy", "") or "").strip().lower() == "stop_loss_monitor"
                or str(getattr(signal, "signal_id", "") or "").strip().lower().startswith(("stoploss-", "stop_loss-", "profitharvest-"))
            )
            if _is_reduce_only:
                logger.warning(
                    "EXEC DEFER (venue down, no quote for close) | %s %s broker=%s — "
                    "deferring reduce-only close to avoid stale-price fill",
                    signal.symbol, signal.side, signal.broker,
                )
                self.last_skip_reason = "deferred_close_no_quote_venue_down"
                return None
            if self.paper_mode:
                await _stamp_microstructure_shadow(None)
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
            self.last_skip_reason = "broker_unavailable"
            return None

        order = await self._apply_marketable_limit(order, signal, broker)
        order = await self._normalize_order_for_broker(order, signal, broker)
        await _stamp_microstructure_shadow(broker)
        if order.quantity <= 0:
            logger.warning(
                "Execution quantity invalid after broker normalization | signal_id=%s symbol=%s broker=%s qty=%s",
                signal.signal_id,
                signal.symbol,
                signal.broker,
                order.quantity,
            )
            self.last_skip_reason = "invalid_quantity_after_normalization"
            return None

        # D031C — execution-boundary sanity guard. Reject loudly if the order
        # notional about to hit the broker materially exceeds the coordinator's
        # intended final capital. This is a defensive backstop, not the primary
        # sizing mechanism; normal flows never trigger it.
        # Telegram notification is intentionally suppressed: this path can fire
        # per-signal across a batch and would spam the channel. Operators watch
        # the `SIZING GUARD REJECT` CRITICAL logs instead.
        if not self._passes_sizing_boundary_guard(order, signal):
            self.last_skip_reason = "sizing_boundary_guard"
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
            self.last_skip_reason = "execution_precheck_rejected"
            return None

        if self.paper_mode and not self._use_native_paper_orders():
            logger.info(
                "PAPER FILL (local simulation) | %s %s qty=%s broker=%s",
                signal.symbol,
                signal.side,
                order.quantity,
                signal.broker,
            )
            result = await self._simulate_fill(order, signal, broker=broker)
            await self._persist_result(session_factory, order, result, signal)
            return result

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
                self.last_skip_reason = "broker_place_order_failed"
                return None

        if result is None:
            self.last_skip_reason = "broker_returned_no_result"
            return None

        self._open_orders[order.client_order_id] = result
        tracked = await self._track_fill_status(broker, result)
        if tracked is not None:
            result = tracked
            self._open_orders[order.client_order_id] = tracked
        self._ensure_rejection_metadata(order, result, signal, broker)

        # Track balance-exhaustion across calls so the next signal can
        # short-circuit before round-tripping the broker again.
        bn = (signal.broker or "").strip().lower()
        if result.status in (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED):
            self._clear_broker_balance_exhausted(bn)
        elif result.status == OrderStatus.REJECTED and self._reject_is_insufficient_balance(order):
            self._mark_broker_balance_exhausted(bn, "insufficient_balance reject")

        logger.info("ORDER PLACED | %s | status=%s", result.broker_order_id, result.status)
        await self._persist_result(session_factory, order, result, signal)
        return result

    @staticmethod
    def _use_native_paper_orders() -> bool:
        """Opt into real broker paper-order placement instead of local fills."""
        return os.getenv("EXECUTION_PAPER_USE_BROKER_ORDERS", "").strip().lower() in (
            "1",
            "true",
            "yes",
            "on",
        )

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

    def _stale_price_cfg(self, signal: Signal | None = None) -> tuple[bool, Decimal]:
        """D115/D120 — Paper-mode stale-price gate. Returns ``(enabled, max_drift_bps)``.

        When enabled, ``_simulate_fill`` rejects an opening order whose
        ``signal.suggested_price`` has drifted against the trade direction
        by more than ``max_drift_bps``. Reduce-only / close intents are
        never blocked.
        """
        risk_engine = get_risk_engine()
        cfg = getattr(risk_engine, "config", {}) if risk_engine is not None else {}
        sp_cfg = cfg.get("stale_price_gate") or {}
        if not isinstance(sp_cfg, dict):
            return (False, Decimal("25"))
        enabled = bool(sp_cfg.get("enabled", True))
        try:
            base_bps = Decimal(str(sp_cfg.get("max_adverse_drift_bps", 25)))
        except Exception:  # noqa: BLE001
            base_bps = Decimal("25")
            
        # D120: Dynamic volatility scaling
        vol_scalar = Decimal("1.0")
        if signal and signal.metadata:
            # We can pull 'symbol_volatility_scalar' or fallback to portfolio 'market_volatility_scalar' if we passed it in signal metadata
            sv = signal.metadata.get("symbol_volatility_scalar")
            if sv is not None:
                try:
                    vol_scalar = Decimal(str(sv))
                except (TypeError, ValueError, InvalidOperation):
                    pass
            
        max_bps = base_bps * (vol_scalar if vol_scalar > 0 else Decimal("1.0"))
        
        return (enabled, max_bps)
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

        sig_md = signal.metadata if isinstance(getattr(signal, "metadata", None), dict) else {}
        reduce_only = bool(
            getattr(order, "reduce_only", False)
            or getattr(signal, "reduce_only", False)
            or sig_md.get("reduce_only")
            or sig_md.get("close_only")
            or str(sig_md.get("coordinator_kind", "")).strip().lower() == "trim_symbol"
        )

        async def _broker_last_price() -> Decimal | None:
            if broker is None:
                return None
            try:
                px = await broker.get_last_price(order.symbol)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Paper fill: get_last_price failed | symbol=%s | %s",
                    order.symbol,
                    exc,
                )
                return None
            try:
                d = Decimal(str(px))
            except Exception:  # noqa: BLE001
                return None
            return d if d > 0 else None

        # ── 0. D115 — stale-price gate ────────────────────────────────────────
        # Opening intents that arrive at execution with a stale
        # ``signal.suggested_price`` (the broker has since moved against the
        # trade direction by more than the configured drift threshold) have
        # already lost their edge. Filling them anyway is the textbook
        # "frictional-loss" pattern observed today: 200+ fills at fixed
        # locked-in prices, each one a small structural loss. Reject the
        # fill so the loop can either re-evaluate at the new price or sit
        # the trade out. Reduce-only and close intents are never blocked.
        if not reduce_only:
            enabled, max_drift_bps = self._stale_price_cfg(signal)
            if enabled and signal.suggested_price is not None and signal.suggested_price > 0:
                market_now = await _broker_last_price()
                if market_now is not None and market_now > 0:
                    drift_abs = abs(market_now - signal.suggested_price)
                    drift_bps = drift_abs / signal.suggested_price * Decimal("10000")
                    if drift_bps > max_drift_bps:
                        adverse = (
                            (order.side == OrderSide.BUY and market_now > signal.suggested_price)
                            or (order.side == OrderSide.SELL and market_now < signal.suggested_price)
                        )
                        if adverse:
                            logger.warning(
                                "Paper fill REJECTED stale_price | symbol=%s side=%s suggested=%s market=%s drift_bps=%.2f threshold=%s",
                                order.symbol,
                                getattr(order.side, "value", order.side),
                                signal.suggested_price,
                                market_now,
                                float(drift_bps),
                                max_drift_bps,
                            )
                            self.last_skip_reason = (
                                f"stale_signal_price_drift_{int(drift_bps)}bps"
                            )
                            return OrderResult(
                                broker_order_id=f"paper-rej-{uuid.uuid4().hex[:12]}",
                                client_order_id=order.client_order_id,
                                status=OrderStatus.REJECTED,
                                symbol=order.symbol,
                                side=order.side,
                                quantity=order.quantity,
                                filled_quantity=Decimal("0"),
                                avg_fill_price=None,
                                fee=Decimal("0"),
                                timestamp=datetime.now(timezone.utc).isoformat(),
                            )

        # ── 1. Determine base fill price ──────────────────────────────────────
        fill_price: Decimal | None = None
        # For reduce-only paper closes, mark the fill at the current market
        # price first. Otherwise closes can realise zero gross P&L simply
        # because the allocator carried the stale entry/signal price.
        if reduce_only:
            fill_price = await _broker_last_price()
            if fill_price is None:
                for k in ("current_price", "close", "last_price", "price"):
                    if k not in sig_md:
                        continue
                    try:
                        px = Decimal(str(sig_md[k]))
                    except Exception:  # noqa: BLE001
                        continue
                    if px > 0:
                        fill_price = px
                        break

        if fill_price is None and signal.suggested_price is not None and signal.suggested_price > 0:
            fill_price = signal.suggested_price
        if fill_price is None and order.limit_price is not None and order.limit_price > 0:
            fill_price = order.limit_price

        if fill_price is None or fill_price <= 0:
            fill_price = await _broker_last_price() or Decimal("0")

        # ── 2. Apply directional slippage ─────────────────────────────────────
        if fill_price is not None and fill_price > 0 and slippage_bps > 0:
            slip_factor = slippage_bps / Decimal("10000")
            if order.side == OrderSide.BUY:
                fill_price = fill_price * (Decimal("1") + slip_factor)
            else:
                fill_price = fill_price * (Decimal("1") - slip_factor)

        # ── 3. Enforce limit price bounds ─────────────────────────────────────
        if (
            not reduce_only
            and order.order_type == OrderType.LIMIT
            and order.limit_price is not None
            and order.limit_price > 0
        ):
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
        """Optionally emit fill Telegram messages when explicitly enabled.

        Default operator preference: Telegram is lifecycle-only. Start/stop
        messages are sent by the orchestrator with balances; execution fills,
        rejects, and failures stay in logs/UI unless MYTBOT_TELEGRAM_EXECUTION_ALERTS
        is deliberately enabled for a diagnostic session.
        """
        try:
            if not self._execution_telegram_enabled():
                return
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

    async def cancel_working_orders(
        self,
        *,
        session_factory=None,
        reason: str = "operator_cancel",
        older_than_sec: float | None = None,
    ) -> int:
        """Cancel DB-tracked working orders and mark broker-confirmed cancels locally.

        ``older_than_sec`` (audit #11): when set, only orders whose timestamp
        is older than this many seconds are cancelled. Used to age-out STALE
        resting limit orders that never filled (e.g. PASSIVE/LIMIT urgency on
        delayed IBKR data) — those orders otherwise perpetually shadow their
        symbol via ``_load_working_order_keys`` so the coordinator can never
        re-propose them, silently starving good opportunities. A fresh
        working order (still likely to fill) is left untouched. In live mode,
        local status changes only after the broker confirms a terminal
        cancel/reject; fills or still-open states remain non-terminal so
        restart dedup keeps blocking duplicates.
        """
        if session_factory is None:
            return 0
        try:
            from sqlalchemy import select, update
            from storage.models import OrderLog
        except Exception as exc:  # noqa: BLE001
            logger.warning("cancel_working_orders imports failed | %s", exc)
            return 0

        cutoff = None
        if older_than_sec is not None and older_than_sec > 0:
            cutoff = datetime.now(timezone.utc) - timedelta(seconds=float(older_than_sec))

        async with session_factory() as session:
            stmt = select(OrderLog).where(
                OrderLog.status.in_(("pending", "open", "partially_filled"))
            )
            if cutoff is not None:
                stmt = stmt.where(OrderLog.timestamp < cutoff)
            q = await session.execute(stmt.order_by(OrderLog.timestamp.asc()))
            rows = list(q.scalars().all())

        cancelled_ids: list[Any] = []
        for row in rows:
            broker_name = str(getattr(row, "broker", "") or "").strip().lower()
            broker_order_id = str(getattr(row, "broker_order_id", "") or "").strip()
            if not broker_name:
                continue
            if not broker_order_id:
                if self.paper_mode:
                    cancelled_ids.append(getattr(row, "id", None))
                continue
            broker = await self._get_broker(broker_name)
            if broker is None:
                logger.warning(
                    "cancel_working_orders broker unavailable | broker=%s id=%s symbol=%s",
                    broker_name,
                    broker_order_id,
                    getattr(row, "symbol", ""),
                )
                continue
            try:
                ok = await broker.cancel_order(broker_order_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "cancel_working_orders failed | broker=%s id=%s symbol=%s | %s",
                    broker_name,
                    broker_order_id,
                    getattr(row, "symbol", ""),
                    exc,
                )
                continue
            if ok and await self._confirm_broker_cancelled(
                broker,
                broker_order_id,
                broker_name=broker_name,
                symbol=str(getattr(row, "symbol", "") or ""),
            ):
                cancelled_ids.append(getattr(row, "id", None))
                logger.info(
                    "cancel_working_orders cancelled | broker=%s id=%s symbol=%s reason=%s",
                    broker_name,
                    broker_order_id,
                    getattr(row, "symbol", ""),
                    reason,
                )

        cancelled_ids = [x for x in cancelled_ids if x is not None]
        if not cancelled_ids:
            return 0
        async with session_factory() as session:
            await session.execute(
                update(OrderLog)
                .where(OrderLog.id.in_(cancelled_ids))
                .values(status="cancelled")
            )
            await session.commit()
        return len(cancelled_ids)

    async def _confirm_broker_cancelled(
        self,
        broker: Any,
        broker_order_id: str,
        *,
        broker_name: str,
        symbol: str,
    ) -> bool:
        """Return True only when broker state confirms terminal cancel/reject."""
        if self.paper_mode:
            return True
        try:
            timeout_sec = float(os.getenv("ORDER_CANCEL_CONFIRM_TIMEOUT_SEC", "10") or "10")
        except Exception:  # noqa: BLE001
            timeout_sec = 10.0
        try:
            interval_sec = float(os.getenv("ORDER_CANCEL_CONFIRM_POLL_SEC", "0.5") or "0.5")
        except Exception:  # noqa: BLE001
            interval_sec = 0.5
        deadline = datetime.now(timezone.utc).timestamp() + max(0.1, timeout_sec)
        terminal_cancel = {"cancelled", "canceled", "rejected", "expired"}
        fill_race = {"filled", "partially_filled"}
        while True:
            try:
                result = await broker.get_order(broker_order_id)
                raw_status = getattr(result, "status", "")
                status = str(getattr(raw_status, "value", raw_status)).strip().lower()
                if status in terminal_cancel:
                    return True
                if status in fill_race:
                    logger.warning(
                        "cancel confirmation saw fill/race | broker=%s id=%s symbol=%s status=%s; local status unchanged",
                        broker_name,
                        broker_order_id,
                        symbol,
                        status,
                    )
                    return False
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "cancel confirmation failed | broker=%s id=%s symbol=%s | %s",
                    broker_name,
                    broker_order_id,
                    symbol,
                    exc,
                )
                return False
            if datetime.now(timezone.utc).timestamp() >= deadline:
                logger.warning(
                    "cancel confirmation timed out | broker=%s id=%s symbol=%s; local status unchanged",
                    broker_name,
                    broker_order_id,
                    symbol,
                )
                return False
            await asyncio.sleep(max(0.05, interval_sec))

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

    async def _cancel_in_flight_order(
        self,
        session_factory,
        existing: Any,
        signal: Signal,
    ) -> bool:
        """Cancel a working same-side order before replacing an operator flatten."""
        broker_order_id = str(getattr(existing, "broker_order_id", "") or "").strip()
        broker_name = (
            str(getattr(existing, "broker", "") or signal.broker or "")
            .strip()
            .lower()
        )
        if not broker_order_id or not broker_name:
            return False
        broker = await self._get_broker(broker_name)
        if broker is None:
            return False
        try:
            ok = await broker.cancel_order(broker_order_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "FLATTEN REPLACE cancel failed | %s %s broker=%s id=%s | %s",
                signal.symbol,
                signal.side,
                broker_name,
                broker_order_id,
                exc,
            )
            return False
        if not ok:
            logger.warning(
                "FLATTEN REPLACE cancel rejected | %s %s broker=%s id=%s",
                signal.symbol,
                signal.side,
                broker_name,
                broker_order_id,
            )
            return False
        if not await self._confirm_broker_cancelled(
            broker,
            broker_order_id,
            broker_name=broker_name,
            symbol=str(signal.symbol),
        ):
            return False
        if session_factory is not None:
            try:
                from sqlalchemy import update
                from storage.models import OrderLog

                async with session_factory() as session:
                    await session.execute(
                        update(OrderLog)
                        .where(OrderLog.id == existing.id)
                        .values(status="cancelled")
                    )
                    await session.commit()
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "FLATTEN REPLACE local cancel mark failed | order=%s | %s",
                    getattr(existing, "id", "?"),
                    exc,
                )
        logger.warning(
            "FLATTEN REPLACE | cancelled existing order %s on %s before market close",
            broker_order_id,
            broker_name,
        )
        return True

    def _build_order(self, signal: Signal) -> Order:
        meta = signal.metadata if isinstance(getattr(signal, "metadata", None), dict) else {}
        inst_meta = dict(meta) if meta else None
        if isinstance(meta.get("option_contract"), dict):
            inst_meta = dict(inst_meta or {})
            inst_meta["instrument_type"] = "option"
            inst_meta["option_contract"] = dict(meta["option_contract"])
            for key in (
                "options_buy_to_open",
                "options_sell_to_open",
                "options_hedge_role",
                "options_paper_only",
            ):
                if key in meta:
                    inst_meta[key] = meta[key]
        spec = parse_option_contract_from_metadata(meta)
        sym = spec.position_key() if spec is not None else signal.symbol
        if spec is not None and bool(meta.get("options_buy_to_open")):
            side = OrderSide.BUY
        elif spec is not None and bool(meta.get("options_sell_to_open")):
            side = OrderSide.SELL
        else:
            side = OrderSide.BUY if signal.side in {"buy", "long"} else OrderSide.SELL
        force_market = bool(
            meta.get("force_market_order")
            or meta.get("flatten_all")
        )
        return Order(
            symbol=sym,
            side=side,
            order_type=OrderType.MARKET if force_market or signal.suggested_price is None else OrderType.LIMIT,
            quantity=signal.suggested_quantity,
            limit_price=None if force_market else signal.suggested_price,
            client_order_id=str(uuid.uuid4()),  # idempotency key
            instrument_metadata=inst_meta,
        )

    def _ensure_rejection_metadata(
        self,
        order: Order,
        result: OrderResult,
        signal: Signal,
        broker: Any,
    ) -> None:
        """Guarantee persisted broker rejections have an operator-visible reason.

        Some adapters can only return the frozen ``OrderResult`` shape, which has
        no reason field. Since the UI reads reasons from ``Order.instrument_metadata``,
        fill a conservative fallback before ``OrderLog`` persistence whenever the
        broker returned a terminal reject/cancel without structured context.
        """
        if result.status not in (OrderStatus.REJECTED, OrderStatus.CANCELLED):
            return
        meta = dict(order.instrument_metadata or {})
        broker_name = str(getattr(broker, "broker_name", "") or signal.broker or "").strip().lower()

        # If the adapter populated an error_message, classify it (so balance
        # exhaustion gets a structured reject_reason) but do not overwrite
        # the human-readable message.
        existing_msg = ""
        for key in ("error_message", "reject_reason", "reason"):
            v = meta.get(key)
            if isinstance(v, str) and v.strip():
                existing_msg = (existing_msg + " " + v).strip().lower()
        already_populated = bool(existing_msg)

        if not already_populated:
            if self.paper_mode and broker_name in {"kraken", "binance", "bybit"}:
                reason = f"{broker_name} adapter has no native paper order placement; order was not sent"
                code = "paper_mode_no_native_order"
            elif result.status == OrderStatus.CANCELLED:
                reason = "Order cancelled without a structured reason; see backend log"
                code = "broker_cancelled_without_reason"
            else:
                reason = "Broker returned rejected without a structured reason; see backend log"
                code = "broker_rejected_without_reason"
            meta["error_message"] = reason
            meta["reject_reason"] = code

        # Always tag who rejected, the terminal status, and a UTC timestamp
        # so operators can audit without correlating to backend logs.
        if broker_name:
            meta.setdefault("rejected_by", broker_name)
        meta.setdefault(
            "terminal_status",
            result.status.value if hasattr(result.status, "value") else str(result.status),
        )
        meta.setdefault("rejected_at", datetime.now(timezone.utc).isoformat())

        # Refine reject_reason when the adapter's free-form error indicates a
        # well-known cause — lets the auto-disable / dashboard logic key off
        # codes instead of substring matching.
        msg = existing_msg + " " + str(meta.get("error_message", "")).lower()
        if "insufficient" in msg and ("balance" in msg or "fund" in msg or "buying power" in msg):
            meta["reject_reason"] = "insufficient_balance"
        elif "exceeds max notional" in msg or "max notional per order" in msg:
            meta["reject_reason"] = "broker_max_notional_exceeded"
        elif "minimum" in msg and ("notional" in msg or "size" in msg or "order" in msg):
            meta["reject_reason"] = "broker_min_notional"

        order.instrument_metadata = meta

    def _reject_is_insufficient_balance(self, order: Order) -> bool:
        meta = order.instrument_metadata if isinstance(order.instrument_metadata, dict) else {}
        code = str(meta.get("reject_reason", "")).strip().lower()
        if code == "insufficient_balance":
            return True
        msg = str(meta.get("error_message", "")).strip().lower()
        return ("insufficient" in msg) and ("balance" in msg or "fund" in msg or "buying power" in msg)

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

    async def _normalize_order_for_broker(self, order: Order, signal: Signal, broker_adapter: Optional[Any] = None) -> Order:
        """
        Apply venue-specific quantity and price constraints before placement.
        """
        broker_name = str(getattr(signal, "broker", "") or "").strip().lower()
        
        if broker_adapter is None:
            broker_adapter = await self._get_broker(broker_name)

        qty = Decimal(str(order.quantity))
        price = order.limit_price

        # Reduce-only/close orders must NOT be floored to whole shares. A
        # fractional residual (e.g. 0.7 sh of an expensive name, common after
        # a partial trim) would ROUND_DOWN to 0, then the qty<=0 guard would
        # silently skip the close — capital gets trapped in a position the
        # system explicitly decided to exit (audit #4). The exit quantity is
        # bounded by the held position, so pass it through exactly; if the
        # venue genuinely can't take the fraction it rejects loudly (visible)
        # rather than us zeroing it out invisibly.
        sig_md = signal.metadata if isinstance(getattr(signal, "metadata", None), dict) else {}
        is_reduce_only = bool(
            getattr(order, "reduce_only", False)
            or getattr(signal, "reduce_only", False)
            or sig_md.get("reduce_only")
            or sig_md.get("close_only")
            or str(sig_md.get("coordinator_kind", "")).strip().lower() in {"trim_symbol", "close_symbol", "flatten_symbol"}
            or str(getattr(signal, "strategy", "") or "").strip().lower() == "stop_loss_monitor"
            or str(getattr(signal, "signal_id", "") or "").strip().lower().startswith(("stoploss-", "stop_loss-", "profitharvest-"))
        )

        if broker_adapter is not None:
            if not is_reduce_only:
                qty = await broker_adapter.quantize_quantity(order.symbol, qty)
            if price is not None:
                price = await broker_adapter.quantize_price(order.symbol, price, side=order.side)
        else:
            # Fallback legacy logic
            if (
                not is_reduce_only
                and broker_name == "ibkr"
                and str(getattr(signal, "asset_class", "") or "").strip().lower() in {"equity", "etf", "bond", "future", "option"}
            ):
                qty = qty.quantize(Decimal("1"), rounding=ROUND_DOWN)

        return replace(order, quantity=qty, limit_price=price)

    async def _get_broker(self, name: str):
        """Lazy-load broker adapter.

        IBKR resilience (#2b): the adapter used to be cached on first
        resolution and returned forever after with NO connectivity recheck —
        so a mid-session IBKR socket drop (its mandatory daily restart /
        weekly re-auth) was never re-detected here and orders were sent into
        a dead socket. Now every resolution first asks the BrokerManager
        whether the venue is available (adapter present + ready + not in a
        maintenance window) and, for a cached adapter, rechecks
        ``is_connected()``. A venue that is down evicts its cache entry and
        returns ``None`` so ``execute()`` skips cleanly
        (``last_skip_reason='broker_disconnected'``) instead of failing an
        order against a broken connection.
        """
        key = (name or "").strip().lower()
        if not key:
            return None

        bm = getattr(self, "_broker_manager", None)
        # Proactive availability gate — covers disconnect AND maintenance
        # window without per-call socket probes when the manager already
        # knows the venue is down.
        if bm is not None:
            is_avail = getattr(bm, "is_broker_available", None)
            if callable(is_avail):
                try:
                    if not is_avail(key):
                        self._brokers.pop(key, None)
                        return None
                except Exception:  # noqa: BLE001
                    pass

        cached = self._brokers.get(key)
        if cached is not None:
            try:
                still_connected = await cached.is_connected()
            except Exception:  # noqa: BLE001
                still_connected = False
            if still_connected:
                return cached
            # Stale cache — venue dropped after it was first resolved.
            logger.warning(
                "Broker cache evicted (no longer connected) | broker=%s", key
            )
            self._brokers.pop(key, None)

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

        # Reduce-only signals are exits — sizing-vs-intent doesn't apply, the
        # quantity is bounded by the existing position. Skip the upstream-intent
        # comparison but still apply absolute caps below.
        is_reduce_only = bool(
            md.get("reduce_only")
            or str(md.get("coordinator_kind", "")).lower().startswith(("close", "flatten", "exit"))
            or str(getattr(signal, "strategy", "") or "").lower() == "stop_loss_monitor"
            or str(getattr(signal, "signal_id", "") or "").lower().startswith(("stoploss-", "stop_loss-"))
        )

        px = order.limit_price if (order.limit_price is not None and order.limit_price > 0) else signal.suggested_price
        if px is None or px <= 0:
            return True
        actual_notional = abs(Decimal(str(order.quantity))) * Decimal(str(px))

        # Optional legacy absolute fallback cap when no upstream sizing audit
        # is attached. Disabled by default because the operator's capital
        # allocation slider is the authoritative deployment target; set
        # EXECUTION_MAX_ORDER_NOTIONAL_USD explicitly to re-enable.
        try:
            absolute_cap = Decimal(os.getenv("EXECUTION_MAX_ORDER_NOTIONAL_USD", "0") or "0")
        except Exception:  # noqa: BLE001
            absolute_cap = Decimal("0")
        if (
            not is_reduce_only
            and absolute_cap > 0
            and actual_notional > absolute_cap
        ):
            logger.critical(
                "SIZING GUARD REJECT (absolute cap) | signal_id=%s symbol=%s side=%s broker=%s | "
                "actual_notional=%s > absolute_cap=%s | sizing_source=%s",
                signal.signal_id,
                signal.symbol,
                signal.side,
                signal.broker,
                actual_notional,
                absolute_cap,
                md.get("sizing_source"),
            )
            return False

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

        enforce_metadata_hard_cap = os.getenv(
            "EXECUTION_ENFORCE_SIZING_HARD_CAP",
            "0",
        ).strip().lower() in ("1", "true", "yes", "on")
        if (
            enforce_metadata_hard_cap
            and not is_reduce_only
            and hard_cap is not None
            and hard_cap > 0
            and actual_notional > hard_cap
        ):
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

        symbol_vol_scalar = Decimal("1.0")
        if isinstance(order.instrument_metadata, dict) and "symbol_volatility_scalar" in order.instrument_metadata:
            try:
                symbol_vol_scalar = Decimal(str(order.instrument_metadata["symbol_volatility_scalar"]))
            except (TypeError, ValueError, InvalidOperation):
                pass
                
        # D120: Scale spread and slippage allowance by asset volatility
        max_spread = limits["max_spread_pct"] * max(Decimal("1.0"), symbol_vol_scalar)
        max_slippage = limits["max_slippage_pct"] * max(Decimal("1.0"), symbol_vol_scalar)

        mid = (best_bid + best_ask) / Decimal("2")
        spread_pct = (best_ask - best_bid) / mid if mid > 0 else Decimal("1")
        if spread_pct > max_spread:
            logger.warning(
                "Spread limit breach | symbol=%s spread_pct=%s max=%s",
                order.symbol,
                spread_pct,
                max_spread,
            )
            return False

        min_liquidity = limits["min_liquidity_usd"] * max(Decimal("0.1"), symbol_vol_scalar)
        book_liquidity = self._book_liquidity_usd(ob)
        if book_liquidity < min_liquidity:
            logger.warning(
                "Liquidity limit breach | symbol=%s liquidity=%s min=%s (scaled from %s by vol %s)",
                order.symbol,
                book_liquidity,
                min_liquidity,
                limits["min_liquidity_usd"],
                symbol_vol_scalar,
            )
            return False

        if order.order_type == OrderType.MARKET:
            slippage_pct = self._estimate_market_slippage_pct(order, ob, mid)
            if slippage_pct > max_slippage:
                logger.warning(
                    "Slippage limit breach | symbol=%s slippage_pct=%s max=%s",
                    order.symbol,
                    slippage_pct,
                    max_slippage,
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
        from sqlalchemy import and_, func, select
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
            local_rows: dict[tuple[str, str], Any] = {}
            paper_position_rows: dict[str, list[Any]] = {}
            async with sf() as session:
                latest_by_key = (
                    select(
                        PositionLog.broker.label("broker"),
                        PositionLog.symbol.label("symbol"),
                        func.max(PositionLog.timestamp).label("max_ts"),
                    )
                    .group_by(PositionLog.broker, PositionLog.symbol)
                    .subquery()
                )
                rows_q = await session.execute(
                    select(PositionLog).join(
                        latest_by_key,
                        and_(
                            PositionLog.broker == latest_by_key.c.broker,
                            PositionLog.symbol == latest_by_key.c.symbol,
                            PositionLog.timestamp == latest_by_key.c.max_ts,
                        ),
                    )
                )
                rows = list(rows_q.scalars().all())
                for row in rows:
                    key = (str(row.broker).strip().lower(), str(row.symbol).strip().upper())
                    qty = Decimal(str(row.quantity))
                    local[key] = local.get(key, Decimal("0")) + qty
                    local_rows[key] = row
                if self.paper_mode:
                    for row in rows:
                        broker_name = str(getattr(row, "broker", "") or "").strip().lower()
                        if broker_name:
                            paper_position_rows.setdefault(broker_name, []).append(row)

            # Ensure we attempt broker reconciliation even before any order execution.
            # Use the union of the construction-time allow-list, configured
            # brokers, and currently connected BrokerManager adapters. Late
            # joiners (notably IBKR after Gateway handshake lag) must be part
            # of the authoritative position snapshot even if they were absent
            # from the initial allow-list.
            preload_names = list(self.allowed_brokers)
            preload_names.extend(str(k).strip().lower() for k in self.broker_configs.keys())
            bm = getattr(self, "_broker_manager", None)
            adapters = getattr(bm, "adapters", None) if bm is not None else None
            if isinstance(adapters, dict):
                preload_names.extend(str(k).strip().lower() for k in adapters.keys())
            preload_names = list(dict.fromkeys(n for n in preload_names if n))
            for broker_name in preload_names:
                if broker_name in self._brokers:
                    continue
                if not self._broker_seems_configured(broker_name):
                    continue
                await self._get_broker(broker_name)

            remote: dict[tuple[str, str], Decimal] = {}
            remote_snapshots: list[tuple[str, Position]] = []
            if self.paper_mode and not self._use_native_paper_orders():
                # Local paper fills live in PositionLog. A broker can be late to
                # join during startup/reconnect, but that must not make the
                # reconciler tombstone simulated positions for that broker.
                for bname, broker_rows in paper_position_rows.items():
                    for row in broker_rows:
                        asset_raw = str(row.asset_class or "equity").strip().lower()
                        try:
                            asset = AssetClass(asset_raw)
                        except Exception:  # noqa: BLE001
                            asset = AssetClass.EQUITY
                        p = Position(
                            symbol=str(row.symbol).strip().upper(),
                            asset_class=asset,
                            quantity=Decimal(str(row.quantity)),
                            avg_entry_price=Decimal(str(row.avg_entry_price)),
                            current_price=Decimal(str(row.current_price)),
                            unrealised_pnl=Decimal(str(row.unrealised_pnl)),
                            broker=bname,
                            instrument_metadata=(
                                row.instrument_metadata
                                if isinstance(row.instrument_metadata, dict)
                                else None
                            ),
                        )
                        key = (bname, p.symbol)
                        remote[key] = remote.get(key, Decimal("0")) + Decimal(str(p.quantity))
                        remote_snapshots.append((bname, p))

            for broker_name, broker in self._brokers.items():
                bname = broker_name.strip().lower()
                if self.paper_mode and not self._use_native_paper_orders():
                    # Paper execution is a local simulated ledger. Broker paper
                    # APIs can be empty, delayed, or session-dependent, so they
                    # must not tombstone locally simulated paper positions.
                    continue
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

            # If broker truth says a symbol no longer exists, persist an
            # explicit zero row. Without this tombstone, latest-position
            # consumers keep resurrecting the previous non-zero snapshot.
            stale_closed: list[tuple[str, Any]] = []
            for key, lq in local.items():
                if abs(lq) <= max_quantity_diff:
                    continue
                if abs(remote.get(key, Decimal("0"))) > max_quantity_diff:
                    continue
                row = local_rows.get(key)
                if row is not None:
                    stale_closed.append((key[0], row))
            if stale_closed:
                snap_ts = datetime.now(timezone.utc)
                async with sf() as session:
                    for broker_name, row in stale_closed:
                        session.add(
                            PositionLog(
                                timestamp=snap_ts,
                                symbol=str(row.symbol).strip().upper()[:72],
                                broker=str(broker_name).strip().lower()[:20],
                                quantity=Decimal("0"),
                                avg_entry_price=Decimal(str(row.avg_entry_price or "0")),
                                current_price=Decimal(str(row.current_price or "0")),
                                unrealised_pnl=Decimal("0"),
                                asset_class=str(row.asset_class or "equity").strip().lower()[:20],
                                instrument_metadata=(
                                    row.instrument_metadata
                                    if isinstance(row.instrument_metadata, dict)
                                    else None
                                ),
                            )
                        )
                    await session.commit()
                logger.warning(
                    "Position reconciliation tombstoned %s stale local rows absent from broker truth",
                    len(stale_closed),
                )

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
        if not self._execution_telegram_enabled():
            return
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

    @staticmethod
    def _execution_telegram_enabled() -> bool:
        enabled = (os.getenv("MYTBOT_TELEGRAM_EXECUTION_ALERTS", "") or "").strip().lower()
        return enabled in ("1", "true", "yes", "on")
