"""
system/orchestrator.py
======================
The single control point for the entire trading system.

States: OFF → STARTING → RUNNING → STOPPING → OFF

Responsibilities:
  - Auto-start infrastructure (Postgres, Redis via Docker)
  - Auto-discover and connect available brokers (skip unavailable)
  - Run the trading loop
  - Run the data pipeline
  - Expose system state for API/UI
  - Graceful shutdown on stop
"""

from __future__ import annotations

import asyncio
import enum
import os
from collections.abc import Awaitable
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from dotenv import load_dotenv
from loguru import logger

from risk.engine import Signal as RiskSignal
from risk.engine import RiskVerdict
from risk.stop_loss import evaluate_stop_loss
from system.broker_manager import BrokerManager, BrokerReport
from system.dependency_manager import DependencyManager, DependencyReport
from system.trading_loop import TradingLoop


class SystemState(str, enum.Enum):
    OFF = "off"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    ERROR = "error"


def _shutdown_step_timeout_sec(env_name: str, default: float) -> float:
    raw = os.getenv(env_name, "").strip()
    if not raw:
        return default
    try:
        return max(0.0, float(raw))
    except ValueError:
        return default


async def _await_shutdown_step(label: str, coro: Awaitable[Any], timeout_sec: float) -> None:
    """Bounded wait so broker I/O cannot strand the system in STOPPING indefinitely."""
    if timeout_sec <= 0:
        await coro
        return
    try:
        await asyncio.wait_for(coro, timeout=timeout_sec)
    except asyncio.TimeoutError:
        logger.warning(
            "orchestrator | shutdown step '{}' timed out after {:.1f}s — continuing OFF transition",
            label,
            timeout_sec,
        )


class Orchestrator:
    """
    One-button system controller.
    Call start() to bring everything up, stop() to tear it down.
    """

    _instance: Orchestrator | None = None

    def __init__(self) -> None:
        load_dotenv()
        self.state = SystemState.OFF
        self.state_changed_at = datetime.now(timezone.utc)
        self.errors: list[str] = []
        self.capital_pct: float = 1.0

        self._dep_manager = DependencyManager(compose_dir=os.getcwd())
        self._dep_report: DependencyReport | None = None

        paper_mode = os.getenv("APP_ENV", "paper").strip().lower() != "live"
        self._broker_manager = BrokerManager(paper_mode=paper_mode)
        self._broker_report: BrokerReport | None = None

        self._trading_loop: TradingLoop | None = None
        self._pipeline_task: asyncio.Task | None = None
        self._pipeline_scan_idx: int = 0
        self._coverage_sync_task: asyncio.Task | None = None
        self._nav_heartbeat_task: asyncio.Task | None = None
        self._stop_loss_task: asyncio.Task | None = None
        self._order_reconcile_task: asyncio.Task | None = None
        # Per-position close throttle to avoid re-emitting closes every monitor tick
        # while broker/order status is still settling.
        self._stop_loss_last_close_ts: dict[str, float] = {}

        self._lock = asyncio.Lock()
        self._last_start_error: str | None = None

    @classmethod
    def get_instance(cls) -> Orchestrator:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def _set_state(self, state: SystemState) -> None:
        prev = self.state
        self.state = state
        self.state_changed_at = datetime.now(timezone.utc)
        logger.info("orchestrator | {} → {}", prev.value, state.value)

    @staticmethod
    async def _sleep_cancellable(total_sec: float, *, chunk_sec: float = 2.0) -> None:
        """Sleep in small slices so asyncio.Task.cancel() stops the pipeline within ~chunk_sec."""
        remaining = max(0.0, float(total_sec))
        ch = max(0.25, min(float(chunk_sec), 30.0))
        while remaining > 0:
            step = min(ch, remaining)
            await asyncio.sleep(step)
            remaining -= step

    async def start(self) -> dict[str, Any]:
        """
        Bring the entire system to RUNNING state.
        Returns a status report.
        """
        async with self._lock:
            if self.state in (SystemState.RUNNING, SystemState.STARTING):
                return self.status()

            self._set_state(SystemState.STARTING)
            self.errors.clear()
            self._last_start_error = None

            try:
                # 1. Infrastructure
                logger.info("orchestrator | ensuring infrastructure...")
                self._dep_report = await self._dep_manager.ensure_all()

                if not self._dep_report.postgres.healthy:
                    err = f"Postgres unavailable: {self._dep_report.postgres.error or 'unknown'}"
                    self.errors.append(err)
                    self._last_start_error = err[:2000]
                    logger.error("orchestrator | {}", err)
                    self._set_state(SystemState.ERROR)
                    return self.status()

                if not self._dep_report.redis.healthy:
                    self.errors.append(f"Redis unavailable: {self._dep_report.redis.error or 'unknown'} (non-blocking)")
                    logger.warning("orchestrator | Redis unavailable — continuing without cache")

                # 2. Database migrations
                await self._run_migrations()

                # 3. Brokers
                logger.info("orchestrator | discovering brokers...")
                self._broker_report = await self._broker_manager.discover_and_connect()

                if not self._broker_report.any_connected:
                    self.errors.append("No brokers connected — running in observation mode")
                    logger.warning("orchestrator | no brokers — observation mode")

                self._broker_manager.start_reconnect_loop()
                self._start_coverage_sync_loop()
                self._start_nav_heartbeat_loop()
                self._start_stop_loss_loop()
                self._start_order_reconcile_loop()

                # 4. Data pipeline (background, non-blocking)
                self._start_pipeline()

                # 5. Trading loop
                paper_mode = os.getenv("APP_ENV", "paper").strip().lower() != "live"
                broker_configs = self._broker_manager.configs
                active_brokers = self._broker_report.active_names if self._broker_report else []

                self._trading_loop = TradingLoop(
                    broker_configs=broker_configs,
                    available_brokers=active_brokers,
                    paper_mode=paper_mode,
                    portfolio_value=float(os.getenv("PORTFOLIO_VALUE", "100000")),
                    loop_interval_sec=int(os.getenv("LOOP_INTERVAL_SEC", "120")),
                    timeframe=os.getenv("TIMEFRAME", "1h"),
                    broker_manager=self._broker_manager,
                    capital_pct=self.capital_pct,
                )
                await self._trading_loop.start()

                from control.runtime import get_risk_engine
                _re = get_risk_engine()
                if _re is not None and _re.is_killed:
                    _re.reset_kill()
                    logger.info("orchestrator | cleared stale kill switch on fresh start")

                self._set_state(SystemState.RUNNING)
                self._last_start_error = None
                logger.info(
                    "orchestrator | RUNNING | brokers={} paper={}",
                    active_brokers or "none (observation)",
                    paper_mode,
                )

            except Exception as exc:
                err = f"Startup failed: {exc}"
                self.errors.append(err)
                self._last_start_error = err[:2000]
                logger.exception("orchestrator | {}", err)
                self._set_state(SystemState.ERROR)

            return self.status()

    async def stop(self) -> dict[str, Any]:
        """
        Gracefully stop all components and return to OFF state.
        """
        async with self._lock:
            if self.state == SystemState.OFF:
                return self.status()

            self._set_state(SystemState.STOPPING)

            # Capture execution engine before clearing the loop (loop holds the live instance).
            tl = self._trading_loop
            execution_engine = getattr(tl, "execution_engine", None) if tl is not None else None

            t_loop = _shutdown_step_timeout_sec("ORCHESTRATOR_STOP_TRADING_LOOP_SEC", 120.0)
            t_cancel = _shutdown_step_timeout_sec("ORCHESTRATOR_STOP_CANCEL_ALL_SEC", 45.0)
            t_pipe = _shutdown_step_timeout_sec("ORCHESTRATOR_STOP_PIPELINE_SEC", 45.0)
            t_disc = _shutdown_step_timeout_sec("ORCHESTRATOR_STOP_DISCONNECT_SEC", 60.0)

            # 1. Stop trading loop (no new signals / iterations)
            if tl is not None:
                try:
                    await _await_shutdown_step("trading_loop.stop", tl.stop(), t_loop)
                except Exception as exc:
                    logger.warning("orchestrator | trading loop stop error: {}", exc)
                self._trading_loop = None

            # 2. Cancel open orders while broker adapters are still connected
            if execution_engine is not None:
                try:
                    await _await_shutdown_step(
                        "execution_engine.cancel_all",
                        execution_engine.cancel_all(),
                        t_cancel,
                    )
                except Exception as exc:
                    logger.warning("orchestrator | cancel_all error: {}", exc)

            # 3. Stop data pipeline task
            if self._pipeline_task is not None and not self._pipeline_task.done():
                self._pipeline_task.cancel()
                try:
                    await _await_shutdown_step(
                        "pipeline_task",
                        self._pipeline_task,
                        t_pipe,
                    )
                except (asyncio.CancelledError, Exception):
                    pass
                self._pipeline_task = None

            # 3b. Stop broker-coverage sync loop
            if self._coverage_sync_task is not None and not self._coverage_sync_task.done():
                self._coverage_sync_task.cancel()
                try:
                    await self._coverage_sync_task
                except (asyncio.CancelledError, Exception):
                    pass
                self._coverage_sync_task = None

            # 3c. Flush one final NAV heartbeat *before* disconnecting brokers so
            # the last row in daily_pnl reflects today's true NAV. Without this,
            # a shutdown between heartbeat ticks would leave the DB with a
            # slightly stale value (worst case: the previous day's row).
            try:
                await asyncio.wait_for(self._flush_nav_heartbeat(), timeout=10.0)
            except Exception as exc:
                logger.warning("orchestrator | final NAV flush error: {}", exc)

            if self._nav_heartbeat_task is not None and not self._nav_heartbeat_task.done():
                self._nav_heartbeat_task.cancel()
                try:
                    await self._nav_heartbeat_task
                except (asyncio.CancelledError, Exception):
                    pass
                self._nav_heartbeat_task = None

            if self._stop_loss_task is not None and not self._stop_loss_task.done():
                self._stop_loss_task.cancel()
                try:
                    await self._stop_loss_task
                except (asyncio.CancelledError, Exception):
                    pass
                self._stop_loss_task = None
            self._stop_loss_last_close_ts.clear()

            if self._order_reconcile_task is not None and not self._order_reconcile_task.done():
                self._order_reconcile_task.cancel()
                try:
                    await self._order_reconcile_task
                except (asyncio.CancelledError, Exception):
                    pass
                self._order_reconcile_task = None

            # 4. Disconnect brokers (cancels reconnect / IBKR background connect)
            try:
                await _await_shutdown_step(
                    "broker_manager.disconnect_all",
                    self._broker_manager.disconnect_all(),
                    t_disc,
                )
            except Exception as exc:
                logger.warning("orchestrator | broker disconnect error: {}", exc)

            # 5. Drop stale globals so /status and kill-switch paths do not see a dead engine
            try:
                from control.runtime import set_execution_engine, set_risk_engine

                set_execution_engine(None)
                set_risk_engine(None)
            except Exception as exc:
                logger.warning("orchestrator | runtime registry clear error: {}", exc)

            self._set_state(SystemState.OFF)
            logger.info("orchestrator | OFF — all components stopped")
            return self.status()

    def status(self) -> dict[str, Any]:
        """Current system status for the API."""
        paper_mode = os.getenv("APP_ENV", "paper").strip().lower() != "live"

        broker_status = self._broker_report.to_dict() if self._broker_report else {}
        active_brokers = self._broker_report.active_names if self._broker_report else []
        dep_status = self._dep_report.to_dict() if self._dep_report else {}

        if self._trading_loop is not None:
            trading_status = self._trading_loop.status_dict()
        else:
            trading_status = {
                "running": False,
                "iterations": 0,
                "last_iteration_at": None,
                "last_error": None,
                "paper_mode": paper_mode,
            }
        if self.state == SystemState.STARTING:
            trading_status = {**trading_status, "orchestrator_starting": True}

        # Surface the loop's strategy registry at the top level so the
        # dashboard can render a full strategy roster (including currently
        # idle ones) instead of only those producing signals *right now*.
        loaded_strategies: list[dict[str, Any]] = []
        ts_ls = trading_status.get("loaded_strategies") if isinstance(trading_status, dict) else None
        if isinstance(ts_ls, list):
            loaded_strategies = ts_ls

        coverage = self._broker_report.coverage() if self._broker_report else {
            "full": False,
            "configured": [],
            "included": [],
            "excluded": [],
        }

        out: dict[str, Any] = {
            "state": self.state.value,
            "state_changed_at": self.state_changed_at.isoformat(),
            "paper_mode": paper_mode,
            "active_brokers": active_brokers,
            "brokers": broker_status,
            "coverage": coverage,
            "infrastructure": dep_status,
            "trading": trading_status,
            "errors": list(self.errors),
            "pipeline_running": self._pipeline_task is not None and not self._pipeline_task.done(),
            "capital_pct": self.capital_pct,
            "loaded_strategies": loaded_strategies,
        }
        if self._last_start_error:
            out["last_start_error"] = self._last_start_error
        return out

    def set_capital_pct(self, pct: float) -> None:
        """Set the fraction of total capital available for trading (0.0 – 1.0)."""
        self.capital_pct = max(0.0, min(1.0, float(pct)))
        if self._trading_loop is not None:
            self._trading_loop.capital_pct = self.capital_pct
        logger.info("orchestrator | capital_pct set to {:.0%}", self.capital_pct)

    async def _run_migrations(self) -> None:
        """Run database migrations / create tables."""
        try:
            from storage.db import init_async_database, dispose_engine
            engine, sf = await init_async_database()
            if engine is not None:
                await dispose_engine(engine)
                logger.info("orchestrator | database schema ready")
            else:
                self.errors.append("Database connection failed during migration")
        except Exception as exc:
            err = f"Migration error: {exc}"
            self.errors.append(err)
            logger.warning("orchestrator | {}", err)

    def _start_pipeline(self) -> None:
        """Start the data pipeline as a background task (non-blocking)."""
        if self._pipeline_task is not None and not self._pipeline_task.done():
            return
        self._pipeline_task = asyncio.create_task(self._pipeline_runner(), name="data-pipeline")

    def _start_coverage_sync_loop(self) -> None:
        """Start the broker-coverage → risk-engine sync task."""
        if self._coverage_sync_task is not None and not self._coverage_sync_task.done():
            return
        self._coverage_sync_task = asyncio.create_task(
            self._coverage_sync_loop(), name="coverage-sync"
        )

    async def _coverage_sync_loop(self) -> None:
        """
        Keep the risk engine's ``disabled_brokers`` set in sync with broker
        coverage.

        A broker that is configured but not ``connected + balance_ready``
        must never receive new orders: its position state is stale and any
        order routed to it would either bounce (disconnected) or deepen
        exposure the operator cannot currently see (mid-flap). This loop
        disables every excluded broker at the risk layer (the same gate
        used by the kill switch) and re-enables brokers the moment they
        come fully back, without requiring a full orchestrator restart.
        """
        try:
            interval = max(1.0, float(os.getenv("COVERAGE_SYNC_INTERVAL_SEC", "5")))
        except (TypeError, ValueError):
            interval = 5.0
        from control.runtime import get_risk_engine

        last_disabled: set[str] = set()
        while True:
            try:
                report = self._broker_report
                if report is None:
                    await self._sleep_cancellable(interval)
                    continue
                risk = get_risk_engine()
                if risk is None:
                    await self._sleep_cancellable(interval)
                    continue
                cov = report.coverage()
                excluded = {str(e.get("name") or "").strip().lower() for e in cov.get("excluded", [])}
                excluded.discard("")
                included = {str(n or "").strip().lower() for n in cov.get("included", [])}
                included.discard("")

                for name in excluded - last_disabled:
                    risk.disable_broker(name)
                    logger.warning(
                        "orchestrator | coverage | disabled '{}' at risk engine (excluded from NAV)",
                        name,
                    )
                for name in last_disabled & included:
                    risk.enable_broker(name)
                    logger.info(
                        "orchestrator | coverage | re-enabled '{}' at risk engine (back in NAV)",
                        name,
                    )
                last_disabled = excluded
            except Exception as exc:  # noqa: BLE001
                logger.warning("orchestrator | coverage sync error (non-fatal): {}", exc)
            try:
                await self._sleep_cancellable(interval)
            except asyncio.CancelledError:
                return

    def _start_nav_heartbeat_loop(self) -> None:
        """Persist today's NAV to ``daily_pnl`` on a cadence (and at shutdown).

        Without this heartbeat, ``daily_pnl`` only gets written when a trade
        fills. A quiet trading day plus an ungraceful shutdown (OS kill, power
        loss) would leave the system with either no row for today or a stale
        one from yesterday — which is what made the operator think £200k had
        evaporated overnight when in fact the live NAV figure was computed
        incorrectly and the DB fallback had nothing fresh to show.
        """
        if self._nav_heartbeat_task is not None and not self._nav_heartbeat_task.done():
            return
        self._nav_heartbeat_task = asyncio.create_task(
            self._nav_heartbeat_loop(), name="nav-heartbeat"
        )

    async def _flush_nav_heartbeat(self) -> None:
        """Upsert today's NAV row once using the current broker-reported equity.

        Used by the heartbeat loop and by ``stop()`` to guarantee the final
        persisted NAV on graceful shutdown.
        """
        try:
            from storage.db import init_async_database, dispose_engine as _dispose
        except Exception as exc:  # noqa: BLE001
            logger.debug("orchestrator | nav heartbeat | db module unavailable: {}", exc)
            return

        eng = None
        try:
            eng, sf = await init_async_database()
            if sf is None:
                return
            from run_m3 import _load_portfolio_state, _upsert_daily_pnl
            from system.portfolio_equity import live_portfolio_value

            total_equity = await live_portfolio_value(self._broker_manager)
            if total_equity <= 0:
                # No broker reported a usable equity figure right now. Skip
                # writing rather than clobber a valid prior row with zero.
                return
            portfolio_state = await _load_portfolio_state(
                sf,
                fallback_portfolio_value=total_equity,
                capital_pct=Decimal(str(self.capital_pct)),
            )
            await _upsert_daily_pnl(sf, portfolio_state)
        except Exception as exc:  # noqa: BLE001
            logger.debug("orchestrator | nav heartbeat upsert error (non-fatal): {}", exc)
        finally:
            if eng is not None:
                try:
                    await _dispose(eng)
                except Exception:
                    pass

    async def _nav_heartbeat_loop(self) -> None:
        try:
            interval = max(5.0, float(os.getenv("NAV_HEARTBEAT_INTERVAL_SEC", "60")))
        except (TypeError, ValueError):
            interval = 60.0
        # Small initial delay so the first heartbeat lands after brokers have
        # had a chance to report balances (avoids an avoidable skip on the
        # very first tick).
        try:
            await self._sleep_cancellable(min(10.0, interval))
        except asyncio.CancelledError:
            return
        while True:
            try:
                await self._flush_nav_heartbeat()
            except Exception as exc:  # noqa: BLE001
                logger.debug("orchestrator | nav heartbeat tick error: {}", exc)
            try:
                await self._sleep_cancellable(interval)
            except asyncio.CancelledError:
                return

    def _start_stop_loss_loop(self) -> None:
        """Start post-open stop-loss monitor task (D031E runtime wiring)."""
        if self._stop_loss_task is not None and not self._stop_loss_task.done():
            return
        self._stop_loss_task = asyncio.create_task(
            self._stop_loss_loop(), name="stop-loss-monitor"
        )

    def _start_order_reconcile_loop(self) -> None:
        """Start the stuck-order reconciliation task.

        ``execution.engine.ExecutionEngine._track_fill_status`` only polls for
        ~10s after placement. Anything that doesn't reach FILLED / CANCELLED /
        REJECTED inside that window is left in ``pending`` / ``open`` /
        ``partially_filled`` forever, even though the broker may have moved
        it on hours ago. Combined with the 7-day in-flight dedup window in
        ``ExecutionEngine``, that one omission can completely halt new
        trading on a symbol/broker pair.
        """
        if self._order_reconcile_task is not None and not self._order_reconcile_task.done():
            return
        self._order_reconcile_task = asyncio.create_task(
            self._order_reconcile_loop(), name="order-reconcile"
        )

    async def _order_reconcile_loop(self) -> None:
        try:
            interval = max(15.0, float(os.getenv("ORDER_RECONCILE_INTERVAL_SEC", "60")))
        except (TypeError, ValueError):
            interval = 60.0
        try:
            min_age_sec = max(30.0, float(os.getenv("ORDER_RECONCILE_MIN_AGE_SEC", "120")))
        except (TypeError, ValueError):
            min_age_sec = 120.0
        try:
            max_age_sec = max(min_age_sec, float(os.getenv("ORDER_RECONCILE_MAX_AGE_SEC", "604800")))
        except (TypeError, ValueError):
            max_age_sec = 604800.0
        try:
            stale_cancel_sec = max(min_age_sec, float(os.getenv("ORDER_RECONCILE_STALE_CANCEL_SEC", "86400")))
        except (TypeError, ValueError):
            stale_cancel_sec = 86400.0
        try:
            batch_limit = max(10, int(os.getenv("ORDER_RECONCILE_BATCH_LIMIT", "100")))
        except (TypeError, ValueError):
            batch_limit = 100

        while True:
            try:
                await self._run_order_reconcile_tick(
                    min_age_sec=min_age_sec,
                    max_age_sec=max_age_sec,
                    stale_cancel_sec=stale_cancel_sec,
                    batch_limit=batch_limit,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("orchestrator | order reconcile tick error (non-fatal): {}", exc)
            try:
                await self._sleep_cancellable(interval)
            except asyncio.CancelledError:
                return

    async def _run_order_reconcile_tick(
        self,
        *,
        min_age_sec: float,
        max_age_sec: float,
        stale_cancel_sec: float,
        batch_limit: int,
    ) -> None:
        if self._broker_manager is None:
            return

        try:
            from sqlalchemy import select, update
            from storage.db import init_async_database, dispose_engine as _dispose
            from storage.models import OrderLog
        except Exception as exc:  # noqa: BLE001
            logger.debug("orchestrator | order reconcile imports unavailable: {}", exc)
            return

        eng = None
        try:
            eng, sf = await init_async_database()
            if sf is None:
                return

            now = datetime.now(timezone.utc)
            from datetime import timedelta as _td
            min_cutoff = now - _td(seconds=min_age_sec)
            max_cutoff = now - _td(seconds=max_age_sec)

            async with sf() as session:
                stmt = (
                    select(OrderLog)
                    .where(
                        OrderLog.status.in_(("pending", "open", "partially_filled")),
                        OrderLog.timestamp <= min_cutoff,
                        OrderLog.timestamp >= max_cutoff,
                    )
                    .order_by(OrderLog.timestamp.asc())
                    .limit(batch_limit)
                )
                rows = list((await session.execute(stmt)).scalars().all())

            if not rows:
                return

            updated = 0
            cancelled = 0
            open_order_cache: dict[str, list[Any] | None] = {}

            async def _broker_open_orders(adapter: Any, broker_name: str) -> list[Any] | None:
                if broker_name in open_order_cache:
                    return open_order_cache[broker_name]
                try:
                    orders = list(await adapter.get_open_orders())
                except Exception as exc:  # noqa: BLE001
                    logger.debug(
                        "orchestrator | order reconcile | open orders fetch failed | broker={} | {}",
                        broker_name,
                        exc,
                    )
                    open_order_cache[broker_name] = None
                    return None
                open_order_cache[broker_name] = orders
                return orders

            def _matches_stored_order(open_order: Any, stored: Any) -> bool:
                osym = str(getattr(open_order, "symbol", "") or "").strip().upper()
                ssym = str(getattr(stored, "symbol", "") or "").strip().upper()
                if osym != ssym:
                    return False
                oside_raw = getattr(open_order, "side", "")
                oside = str(getattr(oside_raw, "value", oside_raw) or "").strip().lower()
                sside = str(getattr(stored, "side", "") or "").strip().lower()
                if oside != sside:
                    return False
                status_raw = getattr(open_order, "status", "")
                status = str(getattr(status_raw, "value", status_raw) or "").strip().lower()
                return status in {"pending", "open", "partially_filled"}

            for row in rows:
                bname = (row.broker or "").strip().lower()
                bid = (row.broker_order_id or "").strip()
                if not bname:
                    continue
                adapter = self._broker_manager.adapters.get(bname)
                if adapter is None:
                    continue

                age_sec = (now - row.timestamp).total_seconds() if row.timestamp else 0.0

                latest_status = None
                latest_filled_qty = None
                latest_avg_fill = None
                latest_fee = None
                if bid:
                    try:
                        latest = await adapter.get_order(bid)
                        if latest is not None:
                            s = getattr(latest, "status", None)
                            latest_status = s.value if hasattr(s, "value") else (str(s) if s is not None else None)
                            latest_filled_qty = getattr(latest, "filled_quantity", None)
                            latest_avg_fill = getattr(latest, "avg_fill_price", None)
                            latest_fee = getattr(latest, "fee", None)
                    except Exception as exc:  # noqa: BLE001
                        # Broker may have purged stale orders; if old enough, mark cancelled locally
                        # so dedup unblocks. Otherwise leave for next pass.
                        logger.debug(
                            "orchestrator | order reconcile | get_order failed | broker={} id={} | {}",
                            bname, bid, exc,
                        )
                        open_orders = await _broker_open_orders(adapter, bname)
                        if open_orders is not None and not any(_matches_stored_order(o, row) for o in open_orders):
                            latest_status = "cancelled"
                        elif age_sec >= stale_cancel_sec:
                            latest_status = "cancelled"
                else:
                    # Older adapter paths can persist a working row before IBKR
                    # has assigned a broker id. Reconcile those rows against the
                    # broker's actual open-order book; if no matching active
                    # order exists, the DB row is stale and must not block dedup.
                    open_orders = await _broker_open_orders(adapter, bname)
                    if open_orders is not None and not any(_matches_stored_order(o, row) for o in open_orders):
                        latest_status = "cancelled"

                if latest_status is None:
                    continue

                ls = latest_status.strip().lower()
                terminal = {"filled", "cancelled", "rejected"}
                if ls not in terminal and ls != "partially_filled":
                    # Not yet terminal at the broker either; skip.
                    if age_sec >= stale_cancel_sec:
                        # Aged past tolerance — try cancelling at the broker so
                        # we don't carry the order forever. Best-effort.
                        try:
                            await adapter.cancel_order(bid)
                            ls = "cancelled"
                            cancelled += 1
                        except Exception as exc:  # noqa: BLE001
                            logger.debug(
                                "orchestrator | order reconcile | cancel failed | broker={} id={} | {}",
                                bname, bid, exc,
                            )
                            continue
                    else:
                        continue

                values: dict[str, Any] = {"status": ls[:20]}
                if latest_filled_qty is not None:
                    try:
                        values["filled_quantity"] = Decimal(str(latest_filled_qty))
                    except Exception:  # noqa: BLE001
                        pass
                if latest_avg_fill is not None:
                    try:
                        values["avg_fill_price"] = Decimal(str(latest_avg_fill))
                    except Exception:  # noqa: BLE001
                        pass
                if latest_fee is not None:
                    try:
                        values["fee"] = Decimal(str(latest_fee))
                    except Exception:  # noqa: BLE001
                        pass

                async with sf() as session:
                    await session.execute(
                        update(OrderLog).where(OrderLog.id == row.id).values(**values)
                    )
                    await session.commit()
                updated += 1

            if updated or cancelled:
                logger.info(
                    "orchestrator | order reconcile | swept={} updated={} cancelled={}",
                    len(rows), updated, cancelled,
                )
        finally:
            if eng is not None:
                try:
                    from storage.db import dispose_engine as _dispose
                    await _dispose(eng)
                except Exception:  # noqa: BLE001
                    pass

    async def _run_stop_loss_tick(self) -> None:
        """Evaluate live positions against `max_loss_per_trade_pct` and close when breached."""
        tl = self._trading_loop
        if tl is None:
            return
        risk_engine = getattr(tl, "risk_engine", None)
        execution_engine = getattr(tl, "execution_engine", None)
        if risk_engine is None or execution_engine is None:
            return
        if self._broker_manager is None:
            return

        try:
            max_loss_pct = Decimal(str(getattr(risk_engine, "config", {}).get("max_loss_per_trade_pct", "0")))
        except Exception:  # noqa: BLE001
            max_loss_pct = Decimal("0")
        if max_loss_pct <= 0:
            return

        try:
            close_cooldown_sec = max(5.0, float(os.getenv("STOP_LOSS_CLOSE_COOLDOWN_SEC", "60")))
        except (TypeError, ValueError):
            close_cooldown_sec = 60.0

        try:
            from storage.db import init_async_database, dispose_engine as _dispose
            from run_m3 import _load_portfolio_state
            from system.portfolio_equity import live_portfolio_value
        except Exception as exc:  # noqa: BLE001
            logger.debug("orchestrator | stop-loss tick imports unavailable: {}", exc)
            return

        eng = None
        try:
            eng, sf = await init_async_database()
            if sf is None:
                return

            nav = await live_portfolio_value(self._broker_manager)
            if nav <= 0:
                return

            now_ts = datetime.now(timezone.utc).timestamp()

            for broker_name, adapter in self._broker_manager.adapters.items():
                bname = str(broker_name or "").strip().lower()
                if not bname:
                    continue
                if hasattr(risk_engine, "is_broker_disabled") and risk_engine.is_broker_disabled(bname):
                    continue
                try:
                    positions = await adapter.get_positions()
                except Exception as exc:  # noqa: BLE001
                    logger.debug("orchestrator | stop-loss | positions fetch failed | broker={} | {}", bname, exc)
                    continue

                for pos in positions:
                    try:
                        qty = Decimal(str(getattr(pos, "quantity", "0") or "0"))
                    except Exception:  # noqa: BLE001
                        qty = Decimal("0")
                    if qty == 0:
                        continue
                    sym = str(getattr(pos, "symbol", "") or "").strip().upper()
                    if not sym:
                        continue
                    direction = "long" if qty > 0 else "short"
                    close_key = f"{bname}:{sym}:{direction}"
                    last_ts = self._stop_loss_last_close_ts.get(close_key, 0.0)
                    if now_ts - last_ts < close_cooldown_sec:
                        continue

                    try:
                        entry = Decimal(str(getattr(pos, "avg_entry_price", "0") or "0"))
                        current = Decimal(str(getattr(pos, "current_price", "0") or "0"))
                    except Exception:  # noqa: BLE001
                        continue

                    md = dict(getattr(pos, "instrument_metadata", {}) or {})
                    decision = evaluate_stop_loss(
                        symbol=sym,
                        quantity=qty,
                        avg_entry_price=entry,
                        current_price=current,
                        nav=nav,
                        max_loss_per_trade_pct=max_loss_pct,
                        metadata=md,
                    )
                    if not decision.should_close:
                        continue

                    side = "sell" if qty > 0 else "buy"
                    asset_class_raw = getattr(pos, "asset_class", "equity")
                    asset_class = str(getattr(asset_class_raw, "value", asset_class_raw)).lower()
                    signal = RiskSignal(
                        signal_id=f"stoploss-{sym}-{int(now_ts)}",
                        symbol=sym,
                        side=side,
                        strategy="stop_loss_monitor",
                        confidence=1.0,
                        suggested_quantity=abs(qty),
                        suggested_price=current if current > 0 else entry,
                        broker=bname,
                        asset_class=asset_class,
                        timestamp=datetime.now(timezone.utc).isoformat(),
                        metadata={
                            "reduce_only": True,
                            "stop_loss_monitor": True,
                            "stop_loss_reason": decision.reason,
                            "stop_loss_loss_absolute": str(decision.loss_absolute),
                            "stop_loss_loss_pct_of_nav": str(decision.loss_pct),
                            "stop_loss_structural_stop_breached": bool(decision.structural_stop_breached),
                            "stop_loss_structural_stop_price": (
                                str(decision.structural_stop_price)
                                if decision.structural_stop_price is not None
                                else None
                            ),
                        },
                    )

                    portfolio_state = await _load_portfolio_state(
                        sf,
                        fallback_portfolio_value=nav,
                        signal_price_fallback=signal.suggested_price,
                        capital_pct=Decimal(str(self.capital_pct)),
                    )
                    risk_engine.update_high_watermark(
                        Decimal(str(portfolio_state.get("high_watermark_value", nav)))
                    )
                    risk_engine.restore_runtime_state(portfolio_state)
                    risk_decision = await risk_engine.evaluate_and_persist(sf, signal, portfolio_state)
                    if risk_decision.verdict != RiskVerdict.APPROVED:
                        logger.warning(
                            "orchestrator | stop-loss close rejected by risk | broker={} symbol={} reason={}",
                            bname,
                            sym,
                            risk_decision.reason,
                        )
                        self._stop_loss_last_close_ts[close_key] = now_ts
                        continue

                    result = await execution_engine.execute(
                        signal, risk_decision, session_factory=sf
                    )
                    self._stop_loss_last_close_ts[close_key] = now_ts
                    if result is None:
                        logger.warning(
                            "orchestrator | stop-loss close did not execute | broker={} symbol={} reason={}",
                            bname,
                            sym,
                            decision.reason,
                        )
                        continue
                    logger.warning(
                        "orchestrator | stop-loss close submitted | broker={} symbol={} side={} qty={} reason={}",
                        bname,
                        sym,
                        side,
                        abs(qty),
                        decision.reason,
                    )
        except Exception as exc:  # noqa: BLE001
            logger.debug("orchestrator | stop-loss tick error (non-fatal): {}", exc)
        finally:
            if eng is not None:
                try:
                    await _dispose(eng)
                except Exception:
                    pass

    async def _stop_loss_loop(self) -> None:
        """Periodic post-open stop-loss monitor loop."""
        try:
            interval = max(5.0, float(os.getenv("STOP_LOSS_MONITOR_INTERVAL_SEC", "15")))
        except (TypeError, ValueError):
            interval = 15.0

        try:
            await self._sleep_cancellable(min(5.0, interval))
        except asyncio.CancelledError:
            return

        while True:
            try:
                await self._run_stop_loss_tick()
            except Exception as exc:  # noqa: BLE001
                logger.debug("orchestrator | stop-loss monitor error: {}", exc)
            try:
                await self._sleep_cancellable(interval)
            except asyncio.CancelledError:
                return

    async def _pipeline_runner(self) -> None:
        """Periodically run the data pipeline (feature ingestion)."""
        try:
            from data.universe_builder import UniverseBuilder
            from data.pipeline import run_once
            from storage.db import init_async_database, dispose_engine as _dispose
        except ImportError:
            logger.info("orchestrator | data.pipeline not available — skipping pipeline")
            return

        interval = int(os.getenv("PIPELINE_INTERVAL_SEC", "3600"))
        pipeline_cfg = {}
        try:
            import yaml
            from pathlib import Path
            cfg_path = Path("config/data_pipeline.yaml")
            if cfg_path.exists():
                with cfg_path.open(encoding="utf-8") as f:
                    pipeline_cfg = yaml.safe_load(f) or {}
        except Exception as exc:
            logger.warning("orchestrator | pipeline config load error: {}", exc)

        universe_mode = str(pipeline_cfg.get("universe_mode", "static")).strip().lower()
        base_symbols_cfg = pipeline_cfg.get("symbols") if isinstance(pipeline_cfg.get("symbols"), list) else []
        base_symbols = list(dict.fromkeys([str(s).strip() for s in base_symbols_cfg if str(s).strip()]))
        dynamic_cfg = pipeline_cfg.get("dynamic_universe", {}) or {}
        ranking_cfg = dynamic_cfg.get("ranking", {}) or {}
        ranking_on = universe_mode == "dynamic" and bool(ranking_cfg.get("enabled", False))
        universe_builder = UniverseBuilder(
            max_symbols=int(dynamic_cfg.get("max_symbols", 300)),
            ranking=ranking_cfg if ranking_on else {},
        )

        first_pipeline_run = True
        while True:
            eng = None
            try:
                eng, sf = await init_async_database()
                if sf is not None:
                    if first_pipeline_run:
                        logger.info("orchestrator | pipeline | startup flush — running immediately")
                    if universe_mode == "dynamic":
                        if ranking_on:
                            tiers = await universe_builder.build_tiered_universe(self._broker_manager)
                            scan_batch = max(1, int(ranking_cfg.get("scan_batch_size", 50)))
                            sl = list(tiers.scan)
                            start = self._pipeline_scan_idx % max(len(sl), 1)
                            batch: list[str] = []
                            if sl:
                                for j in range(scan_batch):
                                    batch.append(sl[(start + j) % len(sl)])
                                self._pipeline_scan_idx = (start + scan_batch) % len(sl)
                            dynamic_symbols = list(dict.fromkeys(list(tiers.core) + batch))
                            if not dynamic_symbols:
                                fb = pipeline_cfg.get("symbols")
                                if isinstance(fb, list):
                                    dynamic_symbols = [str(s).strip() for s in fb if s and str(s).strip()]
                            if dynamic_symbols:
                                pipeline_cfg["symbols"] = list(dict.fromkeys(base_symbols + dynamic_symbols))
                                logger.info(
                                    "orchestrator | pipeline tiered | core={} batch={} total={}",
                                    len(tiers.core),
                                    len(batch),
                                    len(pipeline_cfg["symbols"]),
                                )
                        else:
                            dynamic_symbols = await universe_builder.build_symbols(self._broker_manager)
                            if dynamic_symbols:
                                pipeline_cfg["symbols"] = list(dict.fromkeys(base_symbols + dynamic_symbols))
                                logger.info(
                                    "orchestrator | pipeline dynamic universe | symbols={}",
                                    len(pipeline_cfg["symbols"]),
                                )
                    await run_once(sf, pipeline_cfg, backfill=False)
                    logger.info("orchestrator | pipeline cycle complete | first_run={}", first_pipeline_run)
                    first_pipeline_run = False
            except Exception as exc:
                logger.warning("orchestrator | pipeline error (non-fatal): {}", exc)
                first_pipeline_run = False  # don't retry-loop on error
            finally:
                if eng is not None:
                    try:
                        await _dispose(eng)
                    except Exception:
                        pass
            try:
                await self._sleep_cancellable(float(interval))
            except asyncio.CancelledError:
                return
