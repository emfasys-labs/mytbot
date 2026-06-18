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
import sys
from collections.abc import Awaitable
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from loguru import logger

from risk.engine import Signal as RiskSignal
from risk.engine import RiskVerdict
from risk.profit_harvest import (
    evaluate_profit_harvest,
    resolve_harvest_thresholds,
    should_defer_profit_harvest_for_redeployment,
)
from risk.stop_loss import evaluate_stop_loss
from system.local_paper_flatten import flatten_local_paper_book
from system.broker_manager import BrokerManager, BrokerReport
from system.dependency_manager import DependencyManager, DependencyReport
from system.telegram_notify import send_lifecycle_notification
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
        # Default to FLAT (0%) so a fresh boot never auto-deploys 100% of NAV
        # before the persisted operator slider has been restored. Earlier
        # default of 1.0 caused forced ``adaptive_shed`` exits after every
        # restart: the system would briefly think it was 100% deployed,
        # open positions to fill that target, then close the largest ones
        # at market loss when the persisted 50% setting finally loaded.
        # The persisted value is loaded eagerly in :meth:`start` BEFORE
        # any allocator action — if loading fails we stay at 0%.
        self.capital_pct: float = 0.0

        self._dep_manager = DependencyManager(compose_dir=os.getcwd())
        self._dep_report: DependencyReport | None = None

        paper_mode = os.getenv("APP_ENV", "paper").strip().lower() != "live"
        self._broker_manager = BrokerManager(paper_mode=paper_mode)
        self._broker_report: BrokerReport | None = None
        try:
            from control.runtime import set_broker_manager
            set_broker_manager(self._broker_manager)
        except Exception:  # noqa: BLE001
            pass

        self._trading_loop: TradingLoop | None = None
        self._pipeline_task: asyncio.Task | None = None
        self._pipeline_scan_idx: int = 0
        self._coverage_sync_task: asyncio.Task | None = None
        self._nav_heartbeat_task: asyncio.Task | None = None
        self._stop_loss_task: asyncio.Task | None = None
        self._profit_harvest_task: asyncio.Task | None = None
        self._intraday_derisk_task: asyncio.Task | None = None
        self._order_reconcile_task: asyncio.Task | None = None
        self._zero_alloc_flatten_task: asyncio.Task | None = None
        # Embedded auto-training scheduler (replaces the standalone Windows
        # scheduled task). One-button principle: `python run.py` is the
        # only command needed; auto-training rides along.
        self._auto_training_task: asyncio.Task | None = None
        self._auto_training_last_run_at: datetime | None = None
        self._auto_training_proc_running: bool = False
        # D125 fix #2 — single shared registry of in-flight derisk closes
        # across the intraday-derisk AND aggregate-derisk loops. The
        # 2026-05-21 BF-B audit caught both loops submitting an identical
        # sell qty=6598.5 within 4.5 seconds — overselling the position
        # by 2×. Key: `f"{broker}:{symbol}"` (direction-agnostic; only
        # one derisk action per symbol allowed per cooldown window).
        # Value: float epoch seconds of last submitted action.
        self._derisk_inflight_ts: dict[str, float] = {}
        # D116 instrument-registry refresh scheduler (constituents + per-broker
        # availability). None until ``start()`` initialises it.
        self._instrument_registry_scheduler: Any = None
        # Per-position close throttle to avoid re-emitting closes every monitor tick
        # while broker/order status is still settling.
        self._stop_loss_last_close_ts: dict[str, float] = {}
        self._profit_harvest_last_action_ts: dict[str, float] = {}
        self._profit_harvest_peak_pnl: dict[str, Decimal] = {}
        self._intraday_derisk_last_action_ts: dict[str, float] = {}
        self._zero_alloc_flatten_last_ts: float = 0.0

        self._lock = asyncio.Lock()
        self._last_start_error: str | None = None
        self._pipeline_wake_event = asyncio.Event()

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
    async def _sleep_cancellable(
        total_sec: float,
        *,
        chunk_sec: float = 2.0,
        wake_event: asyncio.Event | None = None,
    ) -> bool:
        """Sleep in small slices so asyncio.Task.cancel() stops the pipeline within ~chunk_sec,
        or wake up immediately when wake_event is set. Returns True when a wake
        event interrupted the sleep, otherwise False.
        """
        remaining = max(0.0, float(total_sec))
        ch = max(0.25, min(float(chunk_sec), 30.0))
        while remaining > 0:
            if wake_event is not None and wake_event.is_set():
                wake_event.clear()
                return True
            step = min(ch, remaining)
            if wake_event is not None:
                try:
                    await asyncio.wait_for(wake_event.wait(), timeout=step)
                    wake_event.clear()
                    return True
                except (asyncio.TimeoutError, TimeoutError):
                    pass
            else:
                await asyncio.sleep(step)
            remaining -= step
        return False

    PROFIT_HARVEST_PEAKS_STATE_KEY = "risk.profit_harvest.peaks"

    async def _persist_profit_harvest_peaks(self) -> None:
        """Persist current peak P&L per position so an orchestrator restart
        does not silently reset trailing-lock memory back to zero. Without
        this, a +$3K → −$4K round-trip can survive across a restart with no
        record of the prior peak, leaving the trailing lock unable to fire."""
        try:
            from control.command_bus import CommandBus
            from storage.db import init_async_database, dispose_engine as _dispose
        except Exception:  # noqa: BLE001
            return
        if not self._profit_harvest_peak_pnl and not self._profit_harvest_last_action_ts:
            return
        eng = None
        try:
            eng, sf = await init_async_database()
            if sf is None:
                return
            payload = {
                "peaks": {k: str(v) for k, v in self._profit_harvest_peak_pnl.items()},
                "last_action_ts": dict(self._profit_harvest_last_action_ts),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            await CommandBus(sf).set_state(self.PROFIT_HARVEST_PEAKS_STATE_KEY, payload)
        except Exception as exc:  # noqa: BLE001
            logger.debug("orchestrator | profit-harvest peak persist failed: {}", exc)
        finally:
            if eng is not None:
                try:
                    await _dispose(eng)
                except Exception:
                    pass

    async def _load_persisted_profit_harvest_peaks(self) -> None:
        try:
            from control.command_bus import CommandBus
            from storage.db import init_async_database, dispose_engine as _dispose
        except Exception:  # noqa: BLE001
            return
        eng = None
        try:
            eng, sf = await init_async_database()
            if sf is None:
                return
            raw = await CommandBus(sf).get_state(self.PROFIT_HARVEST_PEAKS_STATE_KEY, None)
            if not isinstance(raw, dict):
                return
            peaks = raw.get("peaks") or {}
            if isinstance(peaks, dict):
                for k, v in peaks.items():
                    try:
                        self._profit_harvest_peak_pnl[str(k)] = Decimal(str(v))
                    except Exception:  # noqa: BLE001
                        continue
            last_ts = raw.get("last_action_ts") or {}
            if isinstance(last_ts, dict):
                for k, v in last_ts.items():
                    try:
                        self._profit_harvest_last_action_ts[str(k)] = float(v)
                    except Exception:  # noqa: BLE001
                        continue
            if self._profit_harvest_peak_pnl:
                logger.info(
                    "orchestrator | restored profit-harvest peaks for {} positions",
                    len(self._profit_harvest_peak_pnl),
                )
        except Exception as exc:  # noqa: BLE001
            logger.debug("orchestrator | profit-harvest peak restore failed: {}", exc)
        finally:
            if eng is not None:
                try:
                    await _dispose(eng)
                except Exception:
                    pass

    async def _load_persisted_capital_pct(self) -> None:
        """Restore operator capital allocation from durable control state.

        Strict semantics: when no persisted value exists the orchestrator
        stays at its constructor default (0.0 = flat). Earlier versions
        defaulted to 1.0 here, which meant a restart with an empty
        ``control_state`` row briefly opened the book to 100% before the
        operator's saved 50% setting could load. That triggered forced
        ``adaptive_shed`` exits — closing the largest positions at market
        loss to fit the shrunken cash sleeve. Default-flat plus eager-load
        eliminates the window entirely.
        """
        try:
            from control.command_bus import CAPITAL_ALLOCATION_STATE_KEY, CommandBus
            from storage.db import dispose_engine as _dispose
            from storage.db import init_async_database
        except Exception as exc:  # noqa: BLE001
            logger.debug("orchestrator | capital allocation restore imports unavailable: {}", exc)
            return

        eng = None
        try:
            eng, sf = await init_async_database()
            if sf is None:
                logger.warning(
                    "orchestrator | capital allocation restore: no session factory — staying at {:.0%}",
                    self.capital_pct,
                )
                return
            bus = CommandBus(sf)
            raw = await bus.get_state(CAPITAL_ALLOCATION_STATE_KEY, None)
            if isinstance(raw, dict):
                raw = raw.get("pct")
            if raw is None:
                logger.info(
                    "orchestrator | no persisted capital_pct — staying at {:.0%} (operator must set via /system/capital-allocation)",
                    self.capital_pct,
                )
                return
            self.set_capital_pct(float(raw))
            logger.info("orchestrator | restored capital_pct from control_state: {:.0%}", self.capital_pct)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "orchestrator | capital allocation restore failed — staying at {:.0%} | {}",
                self.capital_pct, exc,
            )
        finally:
            if eng is not None:
                try:
                    await _dispose(eng)
                except Exception:
                    pass

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
                await self._load_persisted_capital_pct()
                await self._load_persisted_profit_harvest_peaks()

                from control.command_bus import CommandBus
                from storage.db import get_session_factory
                from system.deployment import build_deployment_readiness, get_configured_stage

                sf = get_session_factory()
                bus = CommandBus(sf)
                stage_for_start = await get_configured_stage(bus)
                readiness = await build_deployment_readiness(
                    bus=bus,
                    session_factory=sf,
                    requested_stage=stage_for_start,
                )
                blockers = readiness.get("blockers") if isinstance(readiness, dict) else None
                stage = readiness.get("stage") if isinstance(readiness, dict) else "unknown"
                if blockers:
                    reasons = ", ".join(
                        str(b.get("key", "check")) for b in blockers[:6] if isinstance(b, dict)
                    )
                    raise RuntimeError(f"deployment stage {stage} is not start-ready: {reasons}")

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
                self._start_profit_harvest_loop()
                self._start_intraday_derisk_loop()
                self._start_order_reconcile_loop()
                self._start_zero_alloc_flatten_watchdog()
                self._start_auto_training_loop()
                await self._start_instrument_registry_scheduler()

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
                    timeframe=os.getenv("TIMEFRAME", "1d"),  # D158 Phase 1 — proven edge is daily
                    broker_manager=self._broker_manager,
                    capital_pct=self.capital_pct,
                    pipeline_wake_event=self._pipeline_wake_event,
                )
                await self._trading_loop.start()

                from control.runtime import get_risk_engine
                _re = get_risk_engine()
                if _re is not None and _re.is_killed:
                    logger.critical("orchestrator | risk kill switch remains latched after start; use reset_kill control to clear deliberately")

                self._set_state(SystemState.RUNNING)
                self._last_start_error = None
                logger.info(
                    "orchestrator | RUNNING | brokers={} paper={}",
                    active_brokers or "none (observation)",
                    paper_mode,
                )
                asyncio.create_task(send_lifecycle_notification(
                    "started",
                    broker_manager=self._broker_manager,
                    broker_report=self._broker_report,
                    paper_mode=paper_mode,
                    require_full_coverage=True,
                    wait_timeout_sec=float(os.getenv("TELEGRAM_START_READY_TIMEOUT_SEC", "180")),
                ))

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
            paper_mode = os.getenv("APP_ENV", "paper").strip().lower() != "live"

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

            await send_lifecycle_notification(
                "stopped",
                broker_manager=self._broker_manager,
                broker_report=self._broker_report,
                paper_mode=paper_mode,
                require_full_coverage=True,
                wait_timeout_sec=float(os.getenv("TELEGRAM_STOP_READY_TIMEOUT_SEC", "30")),
            )

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

            if self._profit_harvest_task is not None and not self._profit_harvest_task.done():
                self._profit_harvest_task.cancel()
                try:
                    await self._profit_harvest_task
                except (asyncio.CancelledError, Exception):
                    pass
                self._profit_harvest_task = None
            self._profit_harvest_last_action_ts.clear()
            self._profit_harvest_peak_pnl.clear()

            if self._intraday_derisk_task is not None and not self._intraday_derisk_task.done():
                self._intraday_derisk_task.cancel()
                try:
                    await self._intraday_derisk_task
                except (asyncio.CancelledError, Exception):
                    pass
                self._intraday_derisk_task = None
            self._intraday_derisk_last_action_ts.clear()

            if self._order_reconcile_task is not None and not self._order_reconcile_task.done():
                self._order_reconcile_task.cancel()
                try:
                    await self._order_reconcile_task
                except (asyncio.CancelledError, Exception):
                    pass
                self._order_reconcile_task = None

            if self._zero_alloc_flatten_task is not None and not self._zero_alloc_flatten_task.done():
                self._zero_alloc_flatten_task.cancel()
                try:
                    await self._zero_alloc_flatten_task
                except (asyncio.CancelledError, Exception):
                    pass
                self._zero_alloc_flatten_task = None
            self._zero_alloc_flatten_last_ts = 0.0

            if self._auto_training_task is not None and not self._auto_training_task.done():
                self._auto_training_task.cancel()
                try:
                    await self._auto_training_task
                except (asyncio.CancelledError, Exception):
                    pass
                self._auto_training_task = None

            if self._instrument_registry_scheduler is not None:
                try:
                    await self._instrument_registry_scheduler.stop()
                except Exception as exc:
                    logger.debug("orchestrator | instrument-registry scheduler stop error: {}", exc)
                self._instrument_registry_scheduler = None

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
            self._trading_loop.request_iteration("capital_allocation_changed")
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

    OPENING_NAV_STATE_KEY = "nav.opening_snapshot"

    async def _maybe_record_opening_nav(self, sf, snap) -> None:
        """Persist the first *complete* NAV observation, once, forever.

        Records ``{recorded_at, total, per_broker, included}`` to
        ControlState under ``nav.opening_snapshot`` ONLY when the snapshot
        is complete (every included broker reported) and no record exists
        yet. Never overwrites — so it is a true, immutable opening
        baseline that makes "what did each broker start with?" answerable
        from now on. Exception-safe; never disrupts the heartbeat.
        """
        try:
            if not getattr(snap, "complete", False) or snap.value <= 0:
                return
            from control.command_bus import CommandBus

            bus = CommandBus(sf)
            existing = await bus.get_state(self.OPENING_NAV_STATE_KEY, None)
            if isinstance(existing, dict) and existing.get("recorded_at"):
                return  # immutable — already captured
            from datetime import datetime, timezone

            payload = {
                "recorded_at": datetime.now(timezone.utc).isoformat(),
                "total": str(snap.value),
                "per_broker": dict(getattr(snap, "per_broker", {}) or {}),
                "included": list(getattr(snap, "included", ()) or ()),
                "note": (
                    "First complete broker-NAV observation after this "
                    "deployment. NOT necessarily the original day-0 paper "
                    "seed - earlier history predates this instrumentation."
                ),
            }
            await bus.set_state(self.OPENING_NAV_STATE_KEY, payload)
            logger.info(
                "orchestrator | opening-NAV baseline recorded | total={} brokers={}",
                payload["total"],
                payload["per_broker"],
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("orchestrator | opening-nav record skipped: {}", exc)

    async def _refresh_paper_wallets(self, sf) -> None:
        """Recompute synthetic crypto-venue equity from the authoritative
        ledger and persist the snapshot the adapters read. Crypto venues
        have no exchange-native paper account; this is what lets their P&L
        flow into NAV and reconcile with ``daily_pnl``. Exception-safe."""
        try:
            from system.paper_wallet import (
                CRYPTO_PAPER_BROKERS,
                compute_venue_equity,
                crypto_paper_wallet_enabled,
                write_snapshot,
            )

            if not crypto_paper_wallet_enabled():
                return
            async with sf() as session:
                out: dict[str, dict[str, str]] = {}
                for b in sorted(CRYPTO_PAPER_BROKERS):
                    out[b] = await compute_venue_equity(session, b)
            write_snapshot(out)
        except Exception as exc:  # noqa: BLE001 — never disrupt the heartbeat
            logger.debug("orchestrator | paper-wallet refresh skipped: {}", exc)

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
            from system.portfolio_equity import live_portfolio_snapshot

            snap = await live_portfolio_snapshot(self._broker_manager)
            total_equity = snap.value
            if total_equity <= 0:
                # No broker reported a usable equity figure right now. Skip
                # writing rather than clobber a valid prior row with zero.
                return
            # One-time opening-NAV snapshot: the first *complete* (all
            # included brokers reporting) equity observation is recorded
            # permanently so per-broker starting capital is never ambiguous
            # again. Idempotent — never overwrites an existing record.
            await self._maybe_record_opening_nav(sf, snap)
            await self._refresh_paper_wallets(sf)
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

    @staticmethod
    def _read_active_profile_mode() -> str:
        """Mirror ``TradingLoop._read_active_mode`` so the harvester sees the
        same operator-selected mode (defender / trader / hunter) without
        depending on the loop instance."""
        try:
            import json as _json
            from pathlib import Path as _Path

            p = _Path("data/runtime/active_mode.json")
            if p.is_file():
                return str(_json.loads(p.read_text(encoding="utf-8")).get("mode", "trader")).strip().lower()
        except Exception:  # noqa: BLE001
            pass
        return "trader"

    def _start_stop_loss_loop(self) -> None:
        """Start post-open stop-loss monitor task (D031E runtime wiring)."""
        if self._stop_loss_task is not None and not self._stop_loss_task.done():
            return
        self._stop_loss_task = asyncio.create_task(
            self._stop_loss_loop(), name="stop-loss-monitor"
        )

    def _start_profit_harvest_loop(self) -> None:
        """Start post-open profit harvesting monitor task."""
        if self._profit_harvest_task is not None and not self._profit_harvest_task.done():
            return
        self._profit_harvest_task = asyncio.create_task(
            self._profit_harvest_loop(), name="profit-harvest-monitor"
        )

    def _start_intraday_derisk_loop(self) -> None:
        """Start intraday aggregate-derisk monitor task (D115).

        Graduated portfolio-level defence that fires BEFORE the static
        ``max_daily_loss_pct`` kill switch. Reduces exposure on the worst
        losers as intraday drawdown crosses configured tiers.
        """
        if self._intraday_derisk_task is not None and not self._intraday_derisk_task.done():
            return
        self._intraday_derisk_task = asyncio.create_task(
            self._intraday_derisk_loop(), name="intraday-derisk-monitor"
        )

    AUTO_TRAINING_STATE_KEY = "auto_training.last_run_at"

    def _start_auto_training_loop(self) -> None:
        """Start the embedded auto-training scheduler.

        Replaces the legacy standalone Windows scheduled task. Wakes once
        per minute, reads ``config/auto_training.yaml``, and shells out to
        ``scripts/auto_train_models.py`` in a subprocess (process isolation
        so a training crash cannot take down the trading loop) when the
        configured local start time has passed and we have not already
        run since today's start time.
        """
        if self._auto_training_task is not None and not self._auto_training_task.done():
            return
        self._auto_training_task = asyncio.create_task(
            self._auto_training_loop(), name="auto-training-scheduler"
        )

    async def _auto_training_loop(self) -> None:
        await self._load_persisted_auto_training_last_run()
        # Stagger initial wake so we don't fight startup for resources.
        try:
            await self._sleep_cancellable(30.0)
        except asyncio.CancelledError:
            return
        while True:
            try:
                await self._auto_training_tick()
            except Exception as exc:  # noqa: BLE001
                logger.debug("orchestrator | auto-training tick error: {}", exc)
            try:
                await self._sleep_cancellable(60.0)
            except asyncio.CancelledError:
                return

    def _resolve_auto_training_config(self) -> tuple[bool, str, str] | None:
        """Return (enabled, start_time_local 'HH:MM', tz_name) or None on miss."""
        try:
            import yaml  # type: ignore
        except Exception:  # noqa: BLE001
            return None
        cfg_path = Path("config") / "auto_training.yaml"
        if not cfg_path.is_file():
            return None
        try:
            raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        except Exception as exc:  # noqa: BLE001
            logger.debug("orchestrator | auto-training config read failed: {}", exc)
            return None
        section = raw.get("auto_training") if isinstance(raw, dict) else None
        if not isinstance(section, dict):
            return None
        enabled = bool(section.get("enabled", False))
        sched = section.get("schedule") if isinstance(section.get("schedule"), dict) else {}
        start_str = str(sched.get("start_time_local", "03:20")).strip()
        tz_name = str(section.get("timezone", "UTC")).strip() or "UTC"
        return enabled, start_str, tz_name

    async def _auto_training_tick(self) -> None:
        if self._auto_training_proc_running:
            return
        resolved = self._resolve_auto_training_config()
        if resolved is None:
            return
        enabled, start_str, tz_name = resolved
        if not enabled:
            return
        try:
            hh, mm = (int(x) for x in start_str.split(":", 1))
        except (ValueError, AttributeError):
            logger.warning(
                "orchestrator | auto-training start_time_local '{}' is malformed; expected HH:MM",
                start_str,
            )
            return
        try:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo(tz_name)
        except Exception:
            tz = timezone.utc
        now_local = datetime.now(tz)
        scheduled_today = now_local.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if now_local < scheduled_today:
            return
        last = self._auto_training_last_run_at
        if last is not None:
            last_local = last.astimezone(tz) if last.tzinfo else last.replace(tzinfo=timezone.utc).astimezone(tz)
            if last_local >= scheduled_today:
                return
        await self._run_auto_training_job(now_local.astimezone(timezone.utc))

    async def _run_auto_training_job(self, started_utc: datetime) -> None:
        """Launch scripts/auto_train_models.py as a subprocess."""
        self._auto_training_proc_running = True
        try:
            cmd = [sys.executable, "scripts/auto_train_models.py"]
            logger.info("orchestrator | auto-training: launching {}", " ".join(cmd))
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    cwd=str(Path(".").resolve()),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
            except Exception as exc:  # noqa: BLE001
                logger.error("orchestrator | auto-training: subprocess launch failed: {}", exc)
                return
            try:
                stdout_bytes, _ = await proc.communicate()
            except asyncio.CancelledError:
                proc.terminate()
                raise
            rc = proc.returncode
            tail = (stdout_bytes or b"").decode("utf-8", errors="replace").splitlines()[-20:]
            if rc == 0:
                logger.info(
                    "orchestrator | auto-training: completed rc=0 (tail: {})",
                    " | ".join(tail[-3:]),
                )
            else:
                logger.warning(
                    "orchestrator | auto-training: exited rc={} (tail: {})",
                    rc,
                    " | ".join(tail[-5:]),
                )
            self._auto_training_last_run_at = started_utc
            await self._persist_auto_training_last_run()
        finally:
            self._auto_training_proc_running = False

    async def _persist_auto_training_last_run(self) -> None:
        try:
            from control.command_bus import CommandBus
            from storage.db import init_async_database, dispose_engine as _dispose
        except Exception:  # noqa: BLE001
            return
        if self._auto_training_last_run_at is None:
            return
        eng = None
        try:
            eng, sf = await init_async_database()
            if sf is None:
                return
            await CommandBus(sf).set_state(
                self.AUTO_TRAINING_STATE_KEY,
                {"last_run_at": self._auto_training_last_run_at.isoformat()},
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("orchestrator | auto-training persist failed: {}", exc)
        finally:
            if eng is not None:
                try:
                    await _dispose(eng)
                except Exception:
                    pass

    async def _load_persisted_auto_training_last_run(self) -> None:
        try:
            from control.command_bus import CommandBus
            from storage.db import init_async_database, dispose_engine as _dispose
        except Exception:  # noqa: BLE001
            return
        eng = None
        try:
            eng, sf = await init_async_database()
            if sf is None:
                return
            raw = await CommandBus(sf).get_state(self.AUTO_TRAINING_STATE_KEY, None)
            if isinstance(raw, dict) and isinstance(raw.get("last_run_at"), str):
                try:
                    self._auto_training_last_run_at = datetime.fromisoformat(raw["last_run_at"])
                    logger.info(
                        "orchestrator | auto-training last_run_at restored: {}",
                        self._auto_training_last_run_at.isoformat(),
                    )
                except ValueError:
                    pass
        except Exception as exc:  # noqa: BLE001
            logger.debug("orchestrator | auto-training restore failed: {}", exc)
        finally:
            if eng is not None:
                try:
                    await _dispose(eng)
                except Exception:
                    pass

    def _derisk_inflight_window_sec(self) -> float:
        """D125 fix #2 — cross-loop derisk dedup window.

        After ANY derisk loop (intraday-derisk or aggregate-derisk)
        successfully submits a close/trim on `(broker, symbol)`, both
        loops must skip that symbol for this many seconds. Prevents
        the 4.5-second double-sell race observed in the 2026-05-21
        BF-B audit. Tunable via env for ops; default 30s comfortably
        covers a paper-fill round trip and a normal IBKR ack.
        """
        try:
            return max(1.0, float(os.getenv("DERISK_INFLIGHT_WINDOW_SEC", "30")))
        except (TypeError, ValueError):
            return 30.0

    @staticmethod
    def _symbol_is_tradeable_now(broker: str, asset_class: str, symbol: str) -> bool:
        """D125 fix #4 — defer derisk action when the venue session is closed.

        Pre-market US equity submissions are guaranteed to bounce off
        the `core.market_session` gate inside the execution engine;
        firing them anyway burns DB writes, log noise, and risk-engine
        cycles. Crypto (24/7) and any asset class the session module
        doesn't recognise default to tradeable so we never falsely
        block a legitimate close.
        """
        try:
            from core.market_session import is_tradeable

            return bool(is_tradeable(str(broker or ""), str(asset_class or ""), str(symbol or "")))
        except Exception:  # noqa: BLE001 — gate must never crash the loop
            return True

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

    async def _start_instrument_registry_scheduler(self) -> None:
        """Start the D116 instrument-registry background refresh.

        Optional: disabled if the registry is disabled by config or via
        ``INSTRUMENT_REGISTRY_ENABLED=0``. Self-isolated: any failure here is
        logged but never blocks orchestrator startup or trading.
        """
        if os.getenv("INSTRUMENT_REGISTRY_ENABLED", "1").strip().lower() in {"0", "false", "no", "off"}:
            return
        if self._instrument_registry_scheduler is not None:
            return
        try:
            from instruments.builder import load_config
            from instruments.scheduler import make_scheduler_from_env
            from storage.db import get_app_database

            cfg = load_config()
            if not cfg.enabled:
                logger.info("orchestrator | instrument-registry disabled by config")
                return

            def _session_factory_provider():
                _, sf = get_app_database()
                return sf

            scheduler = make_scheduler_from_env(_session_factory_provider, self._broker_manager)
            await scheduler.start()
            self._instrument_registry_scheduler = scheduler
            try:
                self._broker_manager.register_connect_callback(scheduler.notify_broker_connected)
            except AttributeError:
                logger.debug(
                    "orchestrator | broker_manager lacks register_connect_callback (pre-D116)"
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug("orchestrator | could not register broker connect callback: {}", exc)
            logger.info("orchestrator | instrument-registry scheduler started")
        except Exception as exc:  # noqa: BLE001
            logger.warning("orchestrator | instrument-registry scheduler failed to start: {}", exc)

    def _start_zero_alloc_flatten_watchdog(self) -> None:
        """Start a paper-mode guard for zero-allocation flatten.

        The normal flatten path lives in the trading-loop iteration body. If
        that loop wedges while the operator has already dragged allocation to
        0%, the safest paper-mode fallback is to flatten the local simulated
        book directly rather than waiting for another strategy tick.
        """
        raw = os.getenv("ZERO_ALLOC_FLATTEN_WATCHDOG_ENABLED", "1").strip().lower()
        if raw in {"0", "false", "no", "off"}:
            return
        if os.getenv("APP_ENV", "paper").strip().lower() == "live":
            return
        if self._zero_alloc_flatten_task is not None and not self._zero_alloc_flatten_task.done():
            return
        self._zero_alloc_flatten_task = asyncio.create_task(
            self._zero_alloc_flatten_watchdog_loop(), name="zero-alloc-flatten-watchdog"
        )

    async def _zero_alloc_flatten_watchdog_loop(self) -> None:
        try:
            interval = max(5.0, float(os.getenv("ZERO_ALLOC_FLATTEN_WATCHDOG_INTERVAL_SEC", "15")))
        except (TypeError, ValueError):
            interval = 15.0
        while True:
            try:
                await self._run_zero_alloc_flatten_watchdog_tick()
            except Exception as exc:  # noqa: BLE001
                logger.warning("orchestrator | zero-allocation flatten watchdog error: {}", exc)
            try:
                await self._sleep_cancellable(interval)
            except asyncio.CancelledError:
                return

    def _zero_alloc_loop_is_stale(self) -> tuple[bool, float | None]:
        tl = self._trading_loop
        if tl is None:
            return False, None
        if not getattr(tl, "is_running", False):
            return True, None

        now = datetime.now(timezone.utc)
        last = getattr(tl, "last_iteration_at", None)
        if last is None:
            try:
                startup_grace = max(
                    0.0,
                    float(os.getenv("ZERO_ALLOC_FLATTEN_STARTUP_GRACE_SEC", "45")),
                )
            except (TypeError, ValueError):
                startup_grace = 45.0
            age = (now - self.state_changed_at).total_seconds()
            return age >= startup_grace, age

        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        age = (now - last.astimezone(timezone.utc)).total_seconds()
        try:
            configured = float(os.getenv("ZERO_ALLOC_FLATTEN_STALE_SEC", "0"))
        except (TypeError, ValueError):
            configured = 0.0
        loop_interval = float(getattr(tl, "loop_interval_sec", 120) or 120)
        stale_sec = configured if configured > 0 else max(180.0, loop_interval * 2.5)
        return age >= stale_sec, age

    async def _run_zero_alloc_flatten_watchdog_tick(self) -> None:
        if self.state != SystemState.RUNNING:
            return
        if os.getenv("APP_ENV", "paper").strip().lower() == "live":
            return
        if Decimal(str(self.capital_pct)) > Decimal("0.000001"):
            return

        now_ts = datetime.now(timezone.utc).timestamp()
        try:
            cooldown = max(30.0, float(os.getenv("ZERO_ALLOC_FLATTEN_COOLDOWN_SEC", "120")))
        except (TypeError, ValueError):
            cooldown = 120.0
        if now_ts - self._zero_alloc_flatten_last_ts < cooldown:
            return

        stale, age = self._zero_alloc_loop_is_stale()
        if not stale:
            return

        self._zero_alloc_flatten_last_ts = now_ts
        result = await flatten_local_paper_book(
            apply=True,
            reason="zero_allocation_watchdog",
        )
        if result.count <= 0:
            return

        logger.warning(
            "orchestrator | zero-allocation watchdog flattened {} local paper position(s) | loop_age_sec={}",
            result.count,
            f"{age:.1f}" if age is not None else "unknown",
        )
        # Leave a clear wake behind; if the loop is merely slow rather than
        # dead, its next tick will republish a zero-exposure dashboard.
        if self._trading_loop is not None:
            self._trading_loop.request_iteration("zero_allocation_watchdog_flattened")

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

            # D120: Load portfolio state early to get regime & volatility
            portfolio_state = await _load_portfolio_state(
                sf,
                fallback_portfolio_value=nav,
                signal_price_fallback=Decimal("0"),
                capital_pct=Decimal(str(self.capital_pct)),
            )

            # Restoring risk engine runtime state early so it's fully populated
            risk_engine.update_high_watermark(
                Decimal(str(portfolio_state.get("high_watermark_value", nav)))
            )
            risk_engine.restore_runtime_state(portfolio_state)

            pmeta = portfolio_state.get("metadata", {})
            market_state_score = float(pmeta.get("market_state_score", 1.0))
            vol_scalar = float(pmeta.get("market_volatility_scalar", 1.0))

            try:
                base_max_loss_pct = Decimal(str(getattr(risk_engine, "config", {}).get("max_loss_per_trade_pct", "0")))
            except Exception:  # noqa: BLE001
                base_max_loss_pct = Decimal("0")

            if base_max_loss_pct <= 0:
                return

            # For position_stop_loss_pct, load from env, then from risk_engine config, default to 0.08
            env_pos_stop = os.getenv("POSITION_STOP_LOSS_PCT")
            if env_pos_stop is not None:
                try:
                    base_position_stop_pct = Decimal(env_pos_stop)
                except Exception:
                    base_position_stop_pct = Decimal("0.08")
            else:
                try:
                    base_position_stop_pct = Decimal(str(getattr(risk_engine, "config", {}).get("position_stop_loss_pct", "0.08")))
                except Exception:
                    base_position_stop_pct = Decimal("0.08")

            # Scale limits dynamically: tighter when regime is poor or volatility is high
            multiplier = Decimal(str(market_state_score)) / max(Decimal("1.0"), Decimal(str(vol_scalar)))
            multiplier = max(Decimal("0.1"), min(Decimal("1.0"), multiplier))

            max_loss_pct = base_max_loss_pct * multiplier
            position_stop_pct = base_position_stop_pct * multiplier

            try:
                close_cooldown_sec = max(5.0, float(os.getenv("STOP_LOSS_CLOSE_COOLDOWN_SEC", "60")))
            except (TypeError, ValueError):
                close_cooldown_sec = 60.0

            now_ts = datetime.now(timezone.utc).timestamp()

            # Evaluate against the mark-swept PositionLog (real prices), NOT
            # adapter.get_positions() (IBKR paper reports entry price when
            # marketPrice is missing → fabricated $0 loss → never cut).
            rows_by_broker = await self._latest_open_position_rows_by_broker(sf)
            # D166 — horizon-aware anti-churn gate + position ages.
            pe_cfg = self._protective_exit_config(risk_engine)
            position_ages = await self._fills_age_seconds_by_symbol(sf) if pe_cfg.enabled else {}
            from risk.protective_exit_gate import should_suppress_protective_exit
            for broker_name, adapter in self._broker_manager.adapters.items():
                bname = str(broker_name or "").strip().lower()
                if not bname:
                    continue
                if hasattr(risk_engine, "is_broker_disabled") and risk_engine.is_broker_disabled(bname):
                    continue
                positions = rows_by_broker.get(bname, [])

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
                    # Venue closed → a stop close cannot fill; skip quietly
                    # (re-evaluates at reopen). The breach persists until
                    # the market opens regardless — attempting every 15s
                    # is pure waste.
                    _ac = getattr(pos, "asset_class", "equity")
                    _acl = str(getattr(_ac, "value", _ac) or "equity").strip().lower()
                    try:
                        from core.market_session import is_tradeable as _is_tradeable

                        if not _is_tradeable(bname, _acl, sym):
                            logger.debug(
                                "stop-loss | skip (venue closed) | {} {}", bname, sym
                            )
                            continue
                    except Exception:  # noqa: BLE001
                        pass
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
                        position_stop_pct=position_stop_pct,
                    )
                    if not decision.should_close:
                        continue

                    # D166 — don't cut a fresh daily-horizon thesis on a soft
                    # position-% stop; a catastrophic NAV/position loss or a
                    # structural ATR stop still fires (portfolio survives first).
                    if pe_cfg.enabled:
                        pos_notional = abs(qty) * entry
                        loss_pct_position = (
                            decision.loss_absolute / pos_notional if pos_notional > 0 else Decimal("0")
                        )
                        suppress, why = should_suppress_protective_exit(
                            config=pe_cfg,
                            age_sec=position_ages.get(f"{bname}:{sym}"),
                            loss_pct_nav=decision.loss_pct,
                            loss_pct_position=loss_pct_position,
                            structural_breach=decision.structural_stop_breached,
                        )
                        if suppress:
                            logger.info(
                                "stop-loss | held (anti-churn:{}) | {} {} age={}s | {}",
                                why, bname, sym, position_ages.get(f"{bname}:{sym}"), decision.reason,
                            )
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
                    # Persist the fill into PositionLog + daily_pnl. See
                    # _persist_fill_to_portfolio_state docstring — without
                    # this, the same breaching position re-triggers next tick.
                    await self._persist_fill_to_portfolio_state(
                        sf=sf, signal=signal, result=result, fallback_nav=nav,
                    )
        except Exception as exc:  # noqa: BLE001
            logger.debug("orchestrator | stop-loss tick error (non-fatal): {}", exc)
        finally:
            if eng is not None:
                try:
                    await _dispose(eng)
                except Exception:
                    pass

    async def _latest_open_position_rows_by_broker(self, sf) -> dict[str, list]:
        """Latest mark-swept ``PositionLog`` row per (broker, symbol), open
        only, grouped by lowercased broker.

        The risk monitors MUST evaluate against this — NOT raw
        ``adapter.get_positions()``. The IBKR paper adapter returns
        ``current_price = avgCost`` (the entry price) whenever
        ``portfolio().marketPrice`` is unavailable, so the monitors saw
        a fabricated $0 loss and never cut anything. The loop's
        mark-to-market sweep (live-oracle backed) keeps PositionLog's
        ``current_price`` real, so this is the trustworthy source —
        consistent with the dashboard's corrected unrealised."""
        out: dict[str, list] = {}
        try:
            from sqlalchemy import and_, func, select
            from storage.models import PositionLog

            latest = (
                select(
                    PositionLog.broker.label("b"),
                    PositionLog.symbol.label("s"),
                    func.max(PositionLog.timestamp).label("mx"),
                )
                .group_by(PositionLog.broker, PositionLog.symbol)
                .subquery()
            )
            async with sf() as session:
                rows = list(
                    (
                        await session.execute(
                            select(PositionLog).join(
                                latest,
                                and_(
                                    PositionLog.broker == latest.c.b,
                                    PositionLog.symbol == latest.c.s,
                                    PositionLog.timestamp == latest.c.mx,
                                ),
                            )
                        )
                    ).scalars().all()
                )
            for r in rows:
                try:
                    if Decimal(str(r.quantity or 0)) == 0:
                        continue
                except Exception:  # noqa: BLE001
                    continue
                b = str(getattr(r, "broker", "") or "").strip().lower()
                if b:
                    out.setdefault(b, []).append(r)
        except Exception as exc:  # noqa: BLE001
            logger.debug("orchestrator | latest-open-positions query failed: {}", exc)
        return out

    async def _fills_age_seconds_by_symbol(self, sf) -> dict[str, "Decimal"]:
        """D166 — age (seconds) of each currently-open position's streak.

        Returns ``{f"{broker}:{symbol}": age_sec}`` computed from the clean
        ``fills`` ledger (the open streak starts after the position was last
        flat). Used by the protective-exit anti-churn gate so the stop-loss /
        intraday-derisk / aggregate-derisk monitors don't cut a fresh
        daily-horizon thesis on intraday noise. Best-effort: any failure
        returns ``{}`` (gate then treats every age as unknown → never
        suppresses → pre-D166 behaviour).
        """
        out: dict[str, Decimal] = {}
        try:
            from sqlalchemy import select
            from storage.models import FillLog
            from risk.protective_exit_gate import position_age_seconds_from_fills

            # Generous recent slice; if a streak is older than this window the
            # oldest seen fill becomes the assumed start → age underestimated →
            # conservative (less suppression). Clean post-reset ledger is small.
            try:
                limit = int(os.getenv("PROTECTIVE_EXIT_FILLS_SCAN_LIMIT", "8000"))
            except (TypeError, ValueError):
                limit = 8000
            async with sf() as session:
                rows = list(
                    (
                        await session.execute(
                            select(
                                FillLog.broker,
                                FillLog.symbol,
                                FillLog.timestamp,
                                FillLog.position_qty_after,
                            )
                            .order_by(FillLog.timestamp.desc())
                            .limit(limit)
                        )
                    ).all()
                )
            grouped: dict[str, list] = {}
            for broker, symbol, ts, qty_after in rows:
                b = str(broker or "").strip().lower()
                s = str(symbol or "").strip().upper()
                if not b or not s:
                    continue
                grouped.setdefault(f"{b}:{s}", []).append(
                    {"timestamp": ts, "position_qty_after": qty_after}
                )
            now = datetime.now(timezone.utc)
            for key, fills in grouped.items():
                age = position_age_seconds_from_fills(fills, now=now)
                if age is not None:
                    out[key] = age
        except Exception as exc:  # noqa: BLE001
            logger.debug("orchestrator | fills-age query failed: {}", exc)
        return out

    def _protective_exit_config(self, risk_engine):
        """Parse the D166 anti-churn gate config from the risk engine config."""
        try:
            from risk.protective_exit_gate import parse_protective_exit_config

            raw = (getattr(risk_engine, "config", {}) or {}).get("protective_exit_min_hold")
            return parse_protective_exit_config(raw)
        except Exception:  # noqa: BLE001
            from risk.protective_exit_gate import ProtectiveExitConfig

            return ProtectiveExitConfig(enabled=False)

    async def _run_aggregate_derisk_tick(self) -> None:
        """Force reduce-only de-risk when the AGGREGATE unrealised loss
        breaches a dynamic NAV/volatility budget.

        The per-trade stop only fires on a single position > ~1% of NAV;
        a book bleeding −$7k across 30 small losers trips nothing. This
        closes the worst losers (reduce-only → exempt from the anti-churn
        governor, like stop-loss) until projected loss is back in budget.
        Mirrors the proven stop-loss submission path; gated by
        AGG_UNREALISED_DERISK (default on)."""
        from risk.aggregate_derisk import (
            PositionLoss,
            aggregate_unrealised,
            derisk_budget,
            derisk_enabled,
            select_derisk_closes,
        )

        if not derisk_enabled():
            return
        tl = self._trading_loop
        if tl is None:
            return
        risk_engine = getattr(tl, "risk_engine", None)
        execution_engine = getattr(tl, "execution_engine", None)
        if risk_engine is None or execution_engine is None or self._broker_manager is None:
            return
        try:
            from storage.db import init_async_database, dispose_engine as _dispose
            from run_m3 import _load_portfolio_state
            from system.portfolio_equity import live_portfolio_value
        except Exception as exc:  # noqa: BLE001
            logger.debug("orchestrator | agg de-risk imports unavailable: {}", exc)
            return
        try:
            close_cooldown_sec = max(5.0, float(os.getenv("STOP_LOSS_CLOSE_COOLDOWN_SEC", "60")))
        except (TypeError, ValueError):
            close_cooldown_sec = 60.0

        eng = None
        try:
            eng, sf = await init_async_database()
            if sf is None:
                return
            nav = await live_portfolio_value(self._broker_manager)
            if nav <= 0:
                return

            pls: list[PositionLoss] = []
            # Trustworthy mark-swept marks (NOT adapter.get_positions(), which
            # is poisoned for IBKR paper — see _latest_open_position_rows_by_broker).
            rows_by_broker = await self._latest_open_position_rows_by_broker(sf)
            for broker_name, adapter in self._broker_manager.adapters.items():
                bname = str(broker_name or "").strip().lower()
                if not bname:
                    continue
                if hasattr(risk_engine, "is_broker_disabled") and risk_engine.is_broker_disabled(bname):
                    continue
                positions = rows_by_broker.get(bname, [])
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
                    # Exclude closed-venue positions from the de-risk
                    # candidate set — a reduce-only close can't fill on a
                    # shut market; it will be reconsidered once it reopens.
                    _ac = getattr(pos, "asset_class", "equity")
                    _acl = str(getattr(_ac, "value", _ac) or "equity").strip().lower()
                    try:
                        from core.market_session import is_tradeable as _is_tradeable

                        if not _is_tradeable(bname, _acl, sym):
                            continue
                    except Exception:  # noqa: BLE001
                        pass
                    try:
                        entry = Decimal(str(getattr(pos, "avg_entry_price", "0") or "0"))
                        current = Decimal(str(getattr(pos, "current_price", "0") or "0"))
                    except Exception:  # noqa: BLE001
                        continue
                    acr = getattr(pos, "asset_class", "equity")
                    pls.append(
                        PositionLoss(
                            broker=bname,
                            symbol=sym,
                            quantity=qty,
                            avg_entry_price=entry,
                            current_price=current,
                            asset_class=str(getattr(acr, "value", acr)).lower(),
                            metadata=dict(getattr(pos, "instrument_metadata", {}) or {}),
                        )
                    )

            # D162 — budget from YAML (daily-horizon), not the intraday-era
            # env default. ``aggregate_derisk.max_unrealised_loss_nav_pct``
            # in config/risk_limits.yaml; env AGG_UNREALISED_DERISK_NAV_PCT
            # still overrides when the YAML key is absent.
            agg_cfg = {}
            try:
                raw_cfg = getattr(risk_engine, "config", {}) or {}
                agg_cfg = raw_cfg.get("aggregate_derisk") or {}
                if not isinstance(agg_cfg, dict):
                    agg_cfg = {}
            except Exception:  # noqa: BLE001
                agg_cfg = {}
            budget_pct = agg_cfg.get("max_unrealised_loss_nav_pct")
            chosen = select_derisk_closes(pls, nav, base_pct=budget_pct)
            if not chosen:
                return

            # D166 — horizon-aware anti-churn gate + position ages.
            pe_cfg = self._protective_exit_config(risk_engine)
            position_ages = await self._fills_age_seconds_by_symbol(sf) if pe_cfg.enabled else {}
            logger.warning(
                "orchestrator | aggregate de-risk | unrealised=%s budget=-%s — closing %d worst loser(s)",
                str(aggregate_unrealised(pls)),
                str(derisk_budget(nav, base_pct=budget_pct)),
                len(chosen),
            )
            now_ts = datetime.now(timezone.utc).timestamp()
            for p in chosen:
                direction = "long" if p.quantity > 0 else "short"
                close_key = f"{p.broker}:{p.symbol}:{direction}"
                if now_ts - self._stop_loss_last_close_ts.get(close_key, 0.0) < close_cooldown_sec:
                    continue
                # D125 fix #2 — also honour the cross-loop derisk lock so
                # we don't oversell when intraday-derisk already fired a
                # trim/close on this symbol seconds earlier.
                inflight_key = f"{p.broker}:{p.symbol}"
                if now_ts - self._derisk_inflight_ts.get(inflight_key, 0.0) < self._derisk_inflight_window_sec():
                    logger.info(
                        "orchestrator | agg de-risk skipped | {} {} | another derisk action within cross-loop lock window",
                        p.broker, p.symbol,
                    )
                    continue
                # D125 fix #4 — never submit a derisk action to a closed
                # session venue. Pre-open we'd burn DB writes + log noise
                # for guaranteed-rejected orders (the 2026-05-21 BF-B
                # pattern: 8 failed derisk attempts between 13:00–13:30
                # UTC before NYSE open). Crypto / forex never blocked.
                if not self._symbol_is_tradeable_now(p.broker, p.asset_class, p.symbol):
                    logger.info(
                        "orchestrator | agg de-risk deferred | {} {} | venue session closed",
                        p.broker, p.symbol,
                    )
                    continue
                # D166 — let a fresh daily-horizon thesis mature; a catastrophic
                # single-position loss still closes (the 20% drawdown breaker
                # remains the ultimate portfolio survival floor).
                if pe_cfg.enabled:
                    from risk.protective_exit_gate import should_suppress_protective_exit
                    _pos_notional = abs(p.quantity) * p.avg_entry_price
                    _loss_abs = -p.unrealised if p.unrealised < 0 else Decimal("0")
                    _loss_nav = (_loss_abs / Decimal(str(nav))) if nav else Decimal("0")
                    _loss_pos = (_loss_abs / _pos_notional) if _pos_notional > 0 else Decimal("0")
                    suppress, why = should_suppress_protective_exit(
                        config=pe_cfg,
                        age_sec=position_ages.get(f"{p.broker}:{p.symbol}"),
                        loss_pct_nav=_loss_nav,
                        loss_pct_position=_loss_pos,
                        structural_breach=False,
                    )
                    if suppress:
                        logger.info(
                            "orchestrator | agg de-risk held (anti-churn:{}) | {} {} age={}s",
                            why, p.broker, p.symbol, position_ages.get(f"{p.broker}:{p.symbol}"),
                        )
                        continue
                side = "sell" if p.quantity > 0 else "buy"
                signal = RiskSignal(
                    signal_id=f"aggderisk-{p.symbol}-{int(now_ts)}",
                    symbol=p.symbol,
                    side=side,
                    strategy="aggregate_derisk",
                    confidence=1.0,
                    suggested_quantity=abs(p.quantity),
                    suggested_price=p.current_price if p.current_price > 0 else p.avg_entry_price,
                    broker=p.broker,
                    asset_class=p.asset_class,
                    timestamp=datetime.now(timezone.utc).isoformat(),
                    metadata={
                        "reduce_only": True,
                        "aggregate_derisk": True,
                        "unrealised": str(p.unrealised),
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
                        "orchestrator | agg de-risk close rejected by risk | {} {} | {}",
                        p.broker, p.symbol, risk_decision.reason,
                    )
                    self._stop_loss_last_close_ts[close_key] = now_ts
                    continue
                result = await execution_engine.execute(signal, risk_decision, session_factory=sf)
                self._stop_loss_last_close_ts[close_key] = now_ts
                if result is None:
                    logger.warning(
                        "orchestrator | agg de-risk close did not execute | {} {}",
                        p.broker, p.symbol,
                    )
                    continue
                logger.warning(
                    "orchestrator | agg de-risk close submitted | {} {} side={} qty={} unrealised={}",
                    p.broker, p.symbol, side, abs(p.quantity), str(p.unrealised),
                )
                # D125 fix #2 — mark the cross-loop lock so intraday-derisk
                # cannot also fire a close/trim on this symbol inside the
                # cooldown window.
                self._derisk_inflight_ts[inflight_key] = now_ts
                # D162 — a forced de-risk flatten must also lock fresh opens,
                # otherwise the orchestrator (whose conviction is unchanged)
                # re-buys the same position minutes later and the round-trip
                # cost is burned for nothing (observed: AUDUSD flattened −$5k
                # at 23:01, re-bought 23:04). Same lock the intraday-derisk
                # tiers use; reduce-only exits remain unaffected by it.
                try:
                    lock_sec = float(agg_cfg.get("open_lock_sec", 900) or 900)
                    if lock_sec > 0 and hasattr(risk_engine, "activate_open_lock"):
                        risk_engine.activate_open_lock(
                            seconds=lock_sec,
                            reason=f"aggregate_derisk:{p.symbol}",
                        )
                except Exception as exc:  # noqa: BLE001
                    logger.debug("orchestrator | agg de-risk open_lock failed: {}", exc)
                await self._persist_fill_to_portfolio_state(
                    sf=sf, signal=signal, result=result, fallback_nav=nav,
                )
        except Exception as exc:  # noqa: BLE001
            logger.debug("orchestrator | aggregate de-risk tick error (non-fatal): {}", exc)
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
                await self._run_aggregate_derisk_tick()
            except Exception as exc:  # noqa: BLE001
                logger.debug("orchestrator | aggregate de-risk error: {}", exc)
            try:
                await self._sleep_cancellable(interval)
            except asyncio.CancelledError:
                return

    async def _run_profit_harvest_tick(self) -> None:
        """Evaluate open positions for profit banking and submit reduce-only trims."""
        tl = self._trading_loop
        if tl is None:
            return
        risk_engine = getattr(tl, "risk_engine", None)
        execution_engine = getattr(tl, "execution_engine", None)
        if risk_engine is None or execution_engine is None:
            return

        cfg = getattr(risk_engine, "config", {}) or {}
        ph_cfg = cfg.get("profit_harvest", {}) if isinstance(cfg.get("profit_harvest", {}), dict) else {}
        if not bool(ph_cfg.get("enabled", True)):
            return

        try:
            close_cooldown_sec = max(5.0, float(ph_cfg.get("close_cooldown_sec", 90)))
        except (TypeError, ValueError):
            close_cooldown_sec = 90.0

        # Backwards-compat: a flat (legacy) config still maps to ``base``.
        if "base" not in ph_cfg and any(
            k in ph_cfg for k in ("min_profit_pct", "full_close_profit_pct")
        ):
            ph_cfg = {
                **ph_cfg,
                "base": {
                    "min_profit_pct": ph_cfg.get("min_profit_pct"),
                    "min_profit_nav_pct": ph_cfg.get("min_profit_nav_pct"),
                    "trim_fraction": ph_cfg.get("trim_fraction"),
                    "full_close_profit_pct": ph_cfg.get("full_close_profit_pct"),
                    "trailing_giveback_pct": ph_cfg.get("trailing_giveback_pct"),
                },
            }

        active_mode = self._read_active_profile_mode()
        timeframe = os.getenv("TIMEFRAME", "1h")

        try:
            from storage.db import init_async_database, dispose_engine as _dispose
            from run_m3 import _load_portfolio_state
            from system.portfolio_equity import live_portfolio_value
            from data.feature_lookup import load_latest_feature_json
        except Exception as exc:  # noqa: BLE001
            logger.debug("orchestrator | profit-harvest tick imports unavailable: {}", exc)
            return

        eng = None
        try:
            eng, sf = await init_async_database()
            if sf is None:
                return
            nav = await live_portfolio_value(self._broker_manager)
            if nav <= 0:
                return
            portfolio_state = await _load_portfolio_state(
                sf,
                fallback_portfolio_value=nav,
                capital_pct=Decimal(str(self.capital_pct)),
            )
            positions = dict(portfolio_state.get("positions") or {})
            if not positions:
                self._profit_harvest_peak_pnl.clear()
                return
            # D168 — horizon-aware harvest anti-churn gate + position ages.
            ph_pe_cfg = self._protective_exit_config(risk_engine)
            ph_position_ages = (
                await self._fills_age_seconds_by_symbol(sf) if ph_pe_cfg.enabled else {}
            )
            defer_harvest_for_redeploy = False
            try:
                runtime = risk_engine.snapshot_runtime_state()
                raw_until = runtime.get("open_lock_until") if isinstance(runtime, dict) else None
                open_lock_active = False
                if isinstance(raw_until, str) and raw_until.strip():
                    until = datetime.fromisoformat(raw_until.replace("Z", "+00:00"))
                    if until.tzinfo is None:
                        until = until.replace(tzinfo=timezone.utc)
                    open_lock_active = datetime.now(timezone.utc) < until.astimezone(timezone.utc)

                from portfolio.global_edge_coordinator import cash_factor_for_asset_class

                ge_cfg = getattr(tl, "_global_edge_cfg", {}) or {}
                cash_overrides = ge_cfg.get("cash_factors") if isinstance(ge_cfg, dict) else None
                cash_deployed = Decimal("0")
                for row in positions.values():
                    if not isinstance(row, dict):
                        continue
                    try:
                        row_qty = abs(Decimal(str(row.get("quantity", "0") or "0")))
                        row_px = Decimal(str(row.get("current_price", "0") or "0"))
                    except Exception:  # noqa: BLE001
                        continue
                    if row_qty <= 0 or row_px <= 0:
                        continue
                    row_ac = str(row.get("asset_class") or "").strip().lower()
                    row_sym = str(row.get("symbol") or "").strip().upper()
                    cash_deployed += row_qty * row_px * cash_factor_for_asset_class(
                        row_ac,
                        cash_overrides,
                        symbol=row_sym,
                    )
                adaptive_cfg = ge_cfg.get("adaptive") if isinstance(ge_cfg, dict) else {}
                try:
                    tolerance_pct = Decimal(str((adaptive_cfg or {}).get("target_tolerance_pct", "0.0025")))
                except Exception:  # noqa: BLE001
                    tolerance_pct = Decimal("0.0025")
                defer_harvest_for_redeploy = should_defer_profit_harvest_for_redeployment(
                    cash_deployed=cash_deployed,
                    nav=nav,
                    capital_pct=Decimal(str(self.capital_pct)),
                    open_lock_active=open_lock_active,
                    open_lock_blocks_redeployment=False,
                    tolerance_pct=tolerance_pct,
                )
                if defer_harvest_for_redeploy:
                    logger.info(
                        "profit-harvest | deferred while redeployment locked | cash_deployed={} target={} open_lock_until={}",
                        cash_deployed,
                        nav * Decimal(str(self.capital_pct)),
                        raw_until,
                    )
            except Exception as exc:  # noqa: BLE001
                logger.debug("profit-harvest | redeploy defer check failed open | {}", exc)

            now_ts = datetime.now(timezone.utc).timestamp()
            active_keys: set[str] = set()
            for pos_key, row in positions.items():
                try:
                    qty = Decimal(str(row.get("quantity", "0") or "0"))
                    entry = Decimal(str(row.get("avg_entry_price", "0") or "0"))
                    current = Decimal(str(row.get("current_price", "0") or "0"))
                except Exception:  # noqa: BLE001
                    continue
                if qty == 0 or entry <= 0 or current <= 0:
                    continue
                sym = str(row.get("symbol") or pos_key).split(":", 1)[-1].strip().upper()
                broker = str(row.get("broker") or "").strip().lower()
                asset_class = str(row.get("asset_class") or "equity").strip().lower()
                if not sym or not broker:
                    continue
                # Venue closed → a harvest close cannot fill anyway. Skip
                # quietly (DEBUG) instead of attempting + logging
                # "did not execute" every cycle (the pre-market spam).
                # It re-evaluates automatically once the venue reopens.
                try:
                    from core.market_session import is_tradeable as _is_tradeable

                    if not _is_tradeable(broker, asset_class, sym):
                        logger.debug(
                            "profit-harvest | skip (venue closed) | {} {}",
                            broker, sym,
                        )
                        continue
                except Exception:  # noqa: BLE001 — gate must never break the tick
                    pass
                # PositionLog can lag or diverge from the fills ledger (especially
                # post data-reset, where PositionLog retained legacy snapshots).
                # The ledger is the race-free authority: if it disagrees on sign
                # or shows the position closed, the reduce-only harvest would
                # be rejected by the oversell guard every 90s. Skip cleanly.
                try:
                    from storage.fills_ledger import position_state as _ledger_state

                    ledger_qty, ledger_fills = await _ledger_state(sf, broker, sym)
                    if ledger_fills > 0:
                        if ledger_qty == 0:
                            logger.debug(
                                "profit-harvest | skip (ledger flat) | {} {} pos_log_qty={}",
                                broker, sym, qty,
                            )
                            continue
                        if (ledger_qty > 0) != (qty > 0):
                            logger.debug(
                                "profit-harvest | skip (ledger sign mismatch) | {} {} pos_log_qty={} ledger_qty={}",
                                broker, sym, qty, ledger_qty,
                            )
                            continue
                except Exception:  # noqa: BLE001 — ledger check must never break the tick
                    pass

                side = "sell" if qty > 0 else "buy"
                harvest_key = f"{broker}:{sym}:{'long' if qty > 0 else 'short'}"
                active_keys.add(harvest_key)
                last_ts = self._profit_harvest_last_action_ts.get(harvest_key, 0.0)
                if now_ts - last_ts < close_cooldown_sec:
                    continue

                direction = Decimal("1") if qty > 0 else Decimal("-1")
                current_profit = direction * (current - entry) * abs(qty)
                prev_peak = self._profit_harvest_peak_pnl.get(harvest_key, Decimal("0"))
                peak = max(prev_peak, current_profit)
                self._profit_harvest_peak_pnl[harvest_key] = peak

                # Per-position vol from latest feature snapshot (atr / close).
                vol_pct: Decimal | None = None
                try:
                    async with sf() as feat_sess:
                        feat = await load_latest_feature_json(feat_sess, sym, timeframe)
                    if feat:
                        atr = feat.get("atr_14")
                        last_close = feat.get("close")
                        if atr is not None and last_close not in (None, 0):
                            cp = Decimal(str(last_close))
                            if cp > 0:
                                vol_pct = abs(Decimal(str(atr))) / cp
                except Exception:  # noqa: BLE001
                    vol_pct = None

                im = row.get("instrument_metadata") if isinstance(row, dict) else None
                overrides = (
                    im.get("profit_harvest")
                    if isinstance(im, dict) and isinstance(im.get("profit_harvest"), dict)
                    else None
                )

                thresholds = resolve_harvest_thresholds(
                    config=ph_cfg,
                    profile_mode=active_mode,
                    volatility_pct=vol_pct,
                    overrides=overrides,
                )

                decision = evaluate_profit_harvest(
                    quantity=qty,
                    avg_entry_price=entry,
                    current_price=current,
                    nav=nav,
                    peak_profit_absolute=peak,
                    min_profit_pct=thresholds.min_profit_pct,
                    min_profit_nav_pct=thresholds.min_profit_nav_pct,
                    trim_fraction=thresholds.trim_fraction,
                    full_close_profit_pct=thresholds.full_close_profit_pct,
                    trailing_giveback_pct=thresholds.trailing_giveback_pct,
                    peak_lock_min_nav_pct=thresholds.peak_lock_min_nav_pct,
                )
                if not decision.should_reduce:
                    continue
                # D168 — suppress a trailing-lock close that would realise a
                # loss / immaterial gain on a position younger than the D166
                # min-hold (pure intraday churn on a daily-horizon thesis).
                # Genuine winners (take-profit, or a lock banking material
                # profit) are always allowed; safety stop-loss/derisk paths
                # are separate and unaffected.
                if ph_pe_cfg.enabled:
                    from risk.profit_harvest import should_suppress_harvest_for_horizon

                    suppress_h, why_h = should_suppress_harvest_for_horizon(
                        decision=decision,
                        age_sec=ph_position_ages.get(f"{broker}:{sym}"),
                        min_hold_sec=ph_pe_cfg.min_hold_sec,
                        nav=nav,
                    )
                    if suppress_h:
                        logger.info(
                            "orchestrator | profit-harvest SUPPRESSED (%s) | broker=%s symbol=%s "
                            "reason=%s profit_abs=%s age=%s",
                            why_h,
                            broker,
                            sym,
                            decision.reason,
                            decision.profit_absolute,
                            ph_position_ages.get(f"{broker}:{sym}"),
                        )
                        continue
                if defer_harvest_for_redeploy:
                    continue

                reduce_qty = (abs(qty) * decision.reduce_fraction).quantize(Decimal("0.00000001"))
                if reduce_qty <= 0:
                    continue
                signal = RiskSignal(
                    signal_id=f"profitharvest-{sym}-{int(now_ts)}",
                    symbol=sym,
                    side=side,
                    strategy="profit_harvest_monitor",
                    confidence=1.0,
                    suggested_quantity=reduce_qty,
                    suggested_price=current,
                    broker=broker,
                    asset_class=asset_class,
                    timestamp=datetime.now(timezone.utc).isoformat(),
                    metadata={
                        "reduce_only": True,
                        "profit_harvest_monitor": True,
                        "profit_harvest_reason": decision.reason,
                        "profit_harvest_reduce_fraction": str(decision.reduce_fraction),
                        "profit_harvest_profit_absolute": str(decision.profit_absolute),
                        "profit_harvest_profit_pct": str(decision.profit_pct),
                        "profit_harvest_profit_pct_of_nav": str(decision.profit_pct_of_nav),
                        "profit_harvest_peak_profit_absolute": str(decision.peak_profit_absolute),
                        "profit_harvest_giveback_fraction": str(decision.giveback_fraction),
                        "profit_harvest_thresholds": {
                            "min_profit_pct": str(thresholds.min_profit_pct),
                            "min_profit_nav_pct": str(thresholds.min_profit_nav_pct),
                            "full_close_profit_pct": str(thresholds.full_close_profit_pct),
                            "trim_fraction": str(thresholds.trim_fraction),
                            "trailing_giveback_pct": str(thresholds.trailing_giveback_pct),
                        },
                        "profit_harvest_inputs": thresholds.inputs,
                    },
                )

                risk_engine.update_high_watermark(
                    Decimal(str(portfolio_state.get("high_watermark_value", nav)))
                )
                risk_engine.restore_runtime_state(portfolio_state)
                risk_decision = await risk_engine.evaluate_and_persist(sf, signal, portfolio_state)
                if risk_decision.verdict != RiskVerdict.APPROVED:
                    logger.warning(
                        "orchestrator | profit-harvest rejected by risk | broker={} symbol={} reason={}",
                        broker,
                        sym,
                        risk_decision.reason,
                    )
                    self._profit_harvest_last_action_ts[harvest_key] = now_ts
                    continue

                result = await execution_engine.execute(signal, risk_decision, session_factory=sf)
                self._profit_harvest_last_action_ts[harvest_key] = now_ts
                if result is None:
                    logger.warning(
                        "orchestrator | profit-harvest did not execute | broker={} symbol={} reason={}",
                        broker,
                        sym,
                        decision.reason,
                    )
                    continue
                logger.warning(
                    "orchestrator | profit-harvest submitted | broker={} symbol={} side={} qty={} reason={} profit={}",
                    broker,
                    sym,
                    side,
                    reduce_qty,
                    decision.reason,
                    decision.profit_absolute,
                )
                # Persist the fill into PositionLog + daily_pnl. Without
                # this, the position never updates and the monitor fires
                # again next tick on the same "still profitable" position.
                await self._persist_fill_to_portfolio_state(
                    sf=sf, signal=signal, result=result, fallback_nav=nav,
                )

            for key in list(self._profit_harvest_peak_pnl.keys()):
                if key not in active_keys:
                    self._profit_harvest_peak_pnl.pop(key, None)
                    self._profit_harvest_last_action_ts.pop(key, None)

            # Persist peaks so trailing-lock memory survives orchestrator restarts.
            try:
                await self._persist_profit_harvest_peaks()
            except Exception as exc:  # noqa: BLE001
                logger.debug("orchestrator | profit-harvest peak persist tick error: {}", exc)
        except Exception as exc:  # noqa: BLE001
            logger.debug("orchestrator | profit-harvest tick error (non-fatal): {}", exc)
        finally:
            if eng is not None:
                try:
                    await _dispose(eng)
                except Exception:
                    pass

    def _resolve_profit_harvest_interval(self) -> float:
        """Mode-aware monitor cadence. Hunter reacts fast; defender is patient."""
        env_raw = os.getenv("PROFIT_HARVEST_MONITOR_INTERVAL_SEC", "").strip()
        if env_raw:
            try:
                v = float(env_raw)
                if v > 0:
                    return max(2.0, v)
            except ValueError:
                pass

        tl = self._trading_loop
        cfg = getattr(getattr(tl, "risk_engine", None), "config", {}) if tl is not None else {}
        ph_cfg = cfg.get("profit_harvest", {}) if isinstance(cfg.get("profit_harvest", {}), dict) else {}
        mode = self._read_active_profile_mode()
        mode_intervals = ph_cfg.get("mode_interval_sec", {}) if isinstance(ph_cfg.get("mode_interval_sec", {}), dict) else {}
        if mode in mode_intervals:
            try:
                return max(2.0, float(mode_intervals[mode]))
            except (TypeError, ValueError):
                pass
        try:
            return max(2.0, float(ph_cfg.get("monitor_interval_sec", 20.0)))
        except (TypeError, ValueError):
            return 20.0

    async def _persist_fill_to_portfolio_state(
        self,
        *,
        sf: Any,
        signal: Any,
        result: Any,
        fallback_nav: Decimal,
    ) -> None:
        """Apply an out-of-band fill (profit-harvest, stop-loss) to PositionLog.

        Without this, monitor-issued closes fill on paper and charge fees
        but the position ledger never updates. The monitor then sees the
        same "still profitable" or "still breaching" position next tick
        and fires another redundant close — 19 phantom closes for one
        symbol observed in production. Mirrors the persistence the main
        trading loop runs after every fill.
        """
        try:
            from run_m3 import (
                _apply_signal_to_portfolio_state,
                _load_portfolio_state,
                _persist_position_snapshot,
                _upsert_daily_pnl,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("orchestrator | post-fill persist import failed: {}", exc)
            return
        try:
            filled_qty = Decimal(str(getattr(result, "filled_quantity", "0") or "0"))
            if filled_qty <= 0:
                return
            avg_fill = getattr(result, "avg_fill_price", None)
            if avg_fill is not None:
                try:
                    avg_d = Decimal(str(avg_fill))
                    if avg_d > 0:
                        signal.suggested_price = avg_d
                except Exception:  # noqa: BLE001
                    pass
            signal.suggested_quantity = filled_qty
            try:
                fee_dec = Decimal(str(getattr(result, "fee", "0") or "0"))
            except Exception:  # noqa: BLE001
                fee_dec = Decimal("0")
            post_trade_state = await _load_portfolio_state(
                sf,
                fallback_portfolio_value=fallback_nav,
                signal_price_fallback=signal.suggested_price,
                capital_pct=Decimal(str(self.capital_pct)),
            )
            post_trade_state["fees_today_delta"] = fee_dec
            _apply_signal_to_portfolio_state(post_trade_state, signal, result)
            await _persist_position_snapshot(sf, post_trade_state)
            await _upsert_daily_pnl(sf, post_trade_state)
        except Exception as exc:  # noqa: BLE001
            logger.warning("orchestrator | post-fill persist failed: {}", exc)

    async def _profit_harvest_loop(self) -> None:
        """Periodic post-open profit harvesting monitor loop."""
        interval = self._resolve_profit_harvest_interval()

        try:
            await self._sleep_cancellable(min(5.0, interval))
        except asyncio.CancelledError:
            return

        while True:
            try:
                await self._run_profit_harvest_tick()
            except Exception as exc:  # noqa: BLE001
                logger.debug("orchestrator | profit-harvest monitor error: {}", exc)
            # Re-resolve every iteration so a runtime mode switch takes effect immediately.
            interval = self._resolve_profit_harvest_interval()
            try:
                await self._sleep_cancellable(interval)
            except asyncio.CancelledError:
                return

    async def _intraday_derisk_loop(self) -> None:
        """Periodic intraday aggregate-derisk monitor loop (D115)."""
        interval = self._resolve_intraday_derisk_interval()
        try:
            await self._sleep_cancellable(min(5.0, interval))
        except asyncio.CancelledError:
            return
        while True:
            try:
                await self._run_intraday_derisk_tick()
            except Exception as exc:  # noqa: BLE001
                logger.debug("orchestrator | intraday-derisk monitor error: {}", exc)
            interval = self._resolve_intraday_derisk_interval()
            try:
                await self._sleep_cancellable(interval)
            except asyncio.CancelledError:
                return

    def _resolve_intraday_derisk_interval(self) -> float:
        env_raw = os.getenv("INTRADAY_DERISK_INTERVAL_SEC", "").strip()
        if env_raw:
            try:
                v = float(env_raw)
                if v > 0:
                    return max(5.0, v)
            except ValueError:
                pass
        tl = self._trading_loop
        cfg = getattr(getattr(tl, "risk_engine", None), "config", {}) if tl is not None else {}
        d = cfg.get("intraday_derisk", {}) if isinstance(cfg.get("intraday_derisk", {}), dict) else {}
        try:
            return max(5.0, float(d.get("monitor_interval_sec", 30.0)))
        except (TypeError, ValueError):
            return 30.0

    async def _run_intraday_derisk_tick(self) -> None:
        """Evaluate aggregate intraday drawdown and emit reduce-only trims (D115)."""
        tl = self._trading_loop
        if tl is None:
            return
        risk_engine = getattr(tl, "risk_engine", None)
        execution_engine = getattr(tl, "execution_engine", None)
        if risk_engine is None or execution_engine is None:
            return

        cfg = getattr(risk_engine, "config", {}) or {}
        d_cfg = cfg.get("intraday_derisk", {}) if isinstance(cfg.get("intraday_derisk", {}), dict) else {}
        if not bool(d_cfg.get("enabled", False)):
            return

        try:
            from risk.intraday_derisk import (
                evaluate_intraday_derisk,
                parse_position_loss_tier,
                parse_tiers,
            )
            from storage.db import init_async_database, dispose_engine as _dispose
            from run_m3 import _load_portfolio_state
            from system.portfolio_equity import live_portfolio_value
            from signals.engine import Signal as RiskSignal
            from risk.engine import RiskVerdict
        except Exception as exc:  # noqa: BLE001
            logger.debug("orchestrator | intraday-derisk imports unavailable: {}", exc)
            return

        tiers = parse_tiers(d_cfg.get("tiers"))
        position_loss_tier = parse_position_loss_tier(d_cfg.get("position_loss_tier"))
        if not tiers and position_loss_tier is None:
            return
        position_dynamic_cfg = (
            d_cfg.get("position_loss_tier", {}).get("dynamic", {})
            if isinstance(d_cfg.get("position_loss_tier"), dict)
            else {}
        )
        dynamic_position_loss = bool(position_dynamic_cfg.get("enabled", False))
        position_loss_notional_reference_pct = None
        single_name_cfg = cfg.get("single_name_notional", {}) if isinstance(cfg.get("single_name_notional"), dict) else {}
        try:
            if bool(single_name_cfg.get("enabled", False)):
                position_loss_notional_reference_pct = Decimal(str(single_name_cfg.get("max_pct_nav", "0")))
        except (TypeError, ValueError, InvalidOperation):
            position_loss_notional_reference_pct = None
        try:
            cooldown_sec = max(5.0, float(d_cfg.get("close_cooldown_sec", 120.0)))
        except (TypeError, ValueError):
            cooldown_sec = 120.0

        eng = None
        try:
            eng, sf = await init_async_database()
            if sf is None:
                return
            nav = await live_portfolio_value(self._broker_manager)
            if nav <= 0:
                return
            portfolio_state = await _load_portfolio_state(
                sf,
                fallback_portfolio_value=nav,
                capital_pct=Decimal(str(self.capital_pct)),
            )
            positions_map = portfolio_state.get("positions") or {}
            if not positions_map:
                return

            # Day P&L = realised today + unrealised on currently held book.
            realised_today = Decimal(str(portfolio_state.get("daily_realized_pnl", "0") or "0"))
            unrealised_now = Decimal("0")
            position_rows: list[dict] = []
            for pos_key, row in positions_map.items():
                if not isinstance(row, dict):
                    continue
                try:
                    qty = Decimal(str(row.get("quantity", "0") or "0"))
                    entry = Decimal(str(row.get("avg_entry_price", "0") or "0"))
                    current = Decimal(str(row.get("current_price", "0") or "0"))
                except Exception:  # noqa: BLE001
                    continue
                if qty == 0 or entry <= 0 or current <= 0:
                    continue
                upnl = (current - entry) * qty
                unrealised_now += upnl
                # Add an unrealised_pnl field plus broker/symbol for the evaluator.
                broker = str(row.get("broker") or "").strip().lower()
                sym = str(row.get("symbol") or pos_key).split(":", 1)[-1].strip().upper()
                position_rows.append(
                    {
                        "broker": broker,
                        "symbol": sym,
                        "quantity": qty,
                        "avg_entry_price": entry,
                        "current_price": current,
                        "asset_class": row.get("asset_class") or "equity",
                        "unrealised_pnl": upnl,
                        "instrument_metadata": row.get("instrument_metadata") if isinstance(row.get("instrument_metadata"), dict) else {},
                    }
                )
            day_pnl = realised_today + unrealised_now
            now_ts = datetime.now(timezone.utc).timestamp()

            # D166 — horizon-aware anti-churn gate + position ages.
            pe_cfg = self._protective_exit_config(risk_engine)
            position_ages = await self._fills_age_seconds_by_symbol(sf) if pe_cfg.enabled else {}

            vol_scalar = Decimal("1.0")
            pmeta = portfolio_state.get("metadata")
            if isinstance(pmeta, dict) and "market_volatility_scalar" in pmeta:
                try:
                    vol_scalar = Decimal(str(pmeta["market_volatility_scalar"]))
                except (TypeError, ValueError, InvalidOperation):
                    pass

            actions, tier, tier_idx = evaluate_intraday_derisk(
                nav=Decimal(str(nav)),
                day_pnl=day_pnl,
                positions=position_rows,
                tiers=tiers,
                cooldown_seconds=cooldown_sec,
                last_action_ts=self._intraday_derisk_last_action_ts,
                now_ts=now_ts,
                portfolio_volatility_scalar=vol_scalar,
                position_loss_tier=position_loss_tier,
                dynamic_position_loss=dynamic_position_loss,
                position_loss_notional_reference_pct=position_loss_notional_reference_pct,
                require_open_book_loss_for_aggregate_actions=bool(
                    d_cfg.get("require_open_book_loss_for_aggregate_actions", True)
                ),
            )
            if tier is not None:
                try:
                    from risk.drawdown_governor import (
                        parse_open_lock_config,
                        recovered_from_tier,
                        should_trigger_open_lock,
                    )

                    lock_cfg = parse_open_lock_config(d_cfg.get("open_lock"))
                    if not should_trigger_open_lock(tier_idx=tier_idx, config=lock_cfg):
                        risk_engine.clear_open_lock("intraday_derisk_below_lock_tier")
                    elif recovered_from_tier(
                        nav=Decimal(str(nav)),
                        day_pnl=day_pnl,
                        tier_threshold_pct=tier.threshold_pct,
                        config=lock_cfg,
                    ):
                        risk_engine.clear_open_lock("intraday_derisk_recovered")
                except Exception as exc:  # noqa: BLE001
                    logger.debug("orchestrator | drawdown open-lock skipped | {}", exc)
            if not actions:
                return
            assert tier is not None
            logger.warning(
                "orchestrator | intraday-derisk tier {} fired | day_pnl={} ({:.4%} of NAV) | actions={}",
                tier_idx, day_pnl, float(day_pnl / nav) if nav else 0.0, len(actions),
            )
            for action in actions:
                # D125 fix #2 — honour the cross-loop derisk lock so
                # aggregate-derisk doesn't fire a second close on a
                # symbol intraday-derisk just acted on (the BF-B 4.5s
                # double-sell pattern).
                inflight_key = f"{action.broker}:{action.symbol}"
                if now_ts - self._derisk_inflight_ts.get(inflight_key, 0.0) < self._derisk_inflight_window_sec():
                    logger.info(
                        "orchestrator | intraday-derisk skipped | {} {} | another derisk action within cross-loop lock window",
                        action.broker, action.symbol,
                    )
                    continue
                # D125 fix #4 — never submit a derisk action to a closed
                # session venue. The 2026-05-21 BF-B audit found ~8
                # failed pre-market attempts before the first successful
                # post-open trim — pure log noise + DB churn.
                if not self._symbol_is_tradeable_now(action.broker, action.asset_class, action.symbol):
                    logger.info(
                        "orchestrator | intraday-derisk deferred | {} {} | venue session closed",
                        action.broker, action.symbol,
                    )
                    continue
                # D166 — let a fresh daily-horizon thesis mature. The most-severe
                # aggregate survival tier (tier_idx 0) and catastrophic single-
                # position losses still fire; the per-position tier (-2) and the
                # milder aggregate tiers are gated by min-hold.
                if pe_cfg.enabled:
                    from risk.protective_exit_gate import (
                        _to_decimal as _ped,
                        should_suppress_protective_exit,
                    )
                    _upnl_abs = abs(_ped(action.metadata.get("position_unrealised_pnl"), Decimal("0")))
                    _loss_nav = (_upnl_abs / Decimal(str(nav))) if nav else Decimal("0")
                    _loss_pos = _ped(action.metadata.get("position_loss_pct"), Decimal("0"))
                    suppress, why = should_suppress_protective_exit(
                        config=pe_cfg,
                        age_sec=position_ages.get(f"{action.broker}:{action.symbol}"),
                        loss_pct_nav=_loss_nav,
                        loss_pct_position=_loss_pos,
                        structural_breach=False,
                        is_most_severe_aggregate_tier=(tier_idx == 0),
                    )
                    if suppress:
                        logger.info(
                            "orchestrator | intraday-derisk held (anti-churn:{}) | {} {} age={}s tier={}",
                            why, action.broker, action.symbol,
                            position_ages.get(f"{action.broker}:{action.symbol}"), tier_idx,
                        )
                        continue
                signal = RiskSignal(
                    signal_id=f"intraday_derisk-{action.symbol}-{int(now_ts)}",
                    symbol=action.symbol,
                    side=action.side,
                    strategy="intraday_derisk_monitor",
                    confidence=1.0,
                    suggested_quantity=action.reduce_quantity,
                    suggested_price=action.current_price,
                    broker=action.broker,
                    asset_class=action.asset_class,
                    timestamp=datetime.now(timezone.utc).isoformat(),
                    metadata={
                        **dict(action.metadata or {}),
                        "intraday_derisk_tier_idx": int(action.severity_tier_idx),
                        "intraday_derisk_tier_threshold": str(action.tier_threshold_pct),
                        "intraday_derisk_trim_fraction": str(action.trim_fraction),
                        "intraday_derisk_reason": action.reason,
                    },
                )
                cool_key = f"{action.broker}:{action.symbol}"
                risk_engine.restore_runtime_state(portfolio_state)
                risk_decision = await risk_engine.evaluate_and_persist(sf, signal, portfolio_state)
                if risk_decision.verdict != RiskVerdict.APPROVED:
                    logger.warning(
                        "orchestrator | intraday-derisk rejected by risk | broker={} symbol={} reason={}",
                        action.broker, action.symbol, risk_decision.reason,
                    )
                    self._intraday_derisk_last_action_ts[cool_key] = now_ts
                    continue
                result = await execution_engine.execute(signal, risk_decision, session_factory=sf)
                self._intraday_derisk_last_action_ts[cool_key] = now_ts
                if result is None:
                    logger.warning(
                        "orchestrator | intraday-derisk did not execute | broker={} symbol={}",
                        action.broker, action.symbol,
                    )
                    continue
                try:
                    from risk.drawdown_governor import (
                        derisk_execution_reduced_exposure,
                        parse_open_lock_config,
                        should_trigger_open_lock,
                    )

                    lock_cfg = parse_open_lock_config(d_cfg.get("open_lock"))
                    if (
                        derisk_execution_reduced_exposure(result)
                        and should_trigger_open_lock(tier_idx=tier_idx, config=lock_cfg)
                    ):
                        risk_engine.activate_open_lock(
                            seconds=lock_cfg.cooldown_sec,
                            reason=f"intraday_derisk_tier_{tier_idx}",
                        )
                except Exception as exc:  # noqa: BLE001
                    logger.debug("orchestrator | drawdown open-lock activation skipped | {}", exc)
                logger.warning(
                    "orchestrator | intraday-derisk submitted | broker={} symbol={} side={} qty={} reason={}",
                    action.broker, action.symbol, action.side, action.reduce_quantity, action.reason,
                )
                # D125 fix #2 — set the cross-loop lock so aggregate-derisk
                # cannot fire a second close on this symbol in the window.
                self._derisk_inflight_ts[inflight_key] = now_ts
                await self._persist_fill_to_portfolio_state(
                    sf=sf, signal=signal, result=result, fallback_nav=Decimal(str(nav)),
                )
        except Exception as exc:  # noqa: BLE001
            logger.debug("orchestrator | intraday-derisk tick error (non-fatal): {}", exc)
        finally:
            if eng is not None:
                try:
                    await _dispose(eng)
                except Exception:
                    pass

    async def _pipeline_runner(self) -> None:
        """Periodically run the data pipeline (feature ingestion)."""
        try:
            from data.universe_builder import BuildTelemetry, UniverseBuilder
            from data.pipeline import run_once
            from storage.db import init_async_database, dispose_engine as _dispose
            from universe.intelligence_builder import build_and_save_universe_intelligence
            from universe.snapshot_service import load_universe_selection_config
            # D117 — adaptive universe-caps wiring.
            from universe.adaptive_caps import (
                AdaptiveCapsBase,
                AdaptiveCapsContext,
                apply_churn_hysteresis,
                compute_adaptive_caps,
                load_adaptive_caps_config,
            )
            from universe.adaptive_context import build_adaptive_caps_context
            from universe.adaptive_state import (
                AdaptiveRuntimeState,
                load_adaptive_state,
                save_adaptive_state,
            )
            from data.universe_tiers import UniverseTiers, save_universe_tiers
            # D118 — self-tuning priority pre-filter wiring.
            from data.universe_score_ages import load_score_ages, save_score_ages
            from data.universe_prefilter import (
                AvailabilityHint,
                compute_priority_scores,
            )
            from data.universe_weight_learner import (
                WeightLearner,
                build_training_rows,
                load_weight_learner_state,
                save_weight_learner_state,
            )
            from data.universe_budget_controller import (
                BudgetController,
                CycleObservation,
                load_budget_state,
                save_budget_state,
            )
            from data.universe_transitions import (
                build_previous_tier_map,
                diff_tiers,
                record_transitions,
            )
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
        universe_intel_cfg = load_universe_selection_config()
        universe_intel_on = bool(universe_intel_cfg.get("enabled", False))
        # D117: cache the static baseline caps separately so the adaptive
        # multiplier always starts from the YAML neutral anchor — without
        # this, a previous adaptive cycle's resolved caps would become the
        # next cycle's "base" and the multiplier would compound.
        baseline_max_symbols = int(dynamic_cfg.get("max_symbols", 300))
        baseline_core_max = int(ranking_cfg.get("core_max", 50))
        baseline_scan_max = int(ranking_cfg.get("scan_max", 250))
        baseline_candidates = int(ranking_cfg.get("max_candidates_to_score", 400))
        adaptive_cfg = load_adaptive_caps_config()
        adaptive_on = bool(adaptive_cfg.enabled) and ranking_on
        adaptive_state_path = None  # let the helper choose the default path
        universe_builder = UniverseBuilder(
            max_symbols=baseline_max_symbols,
            ranking=ranking_cfg if ranking_on else {},
        )

        # D118 — self-tuning priority pre-filter config + persisted state.
        priority_cfg = ranking_cfg.get("priority_score", {}) or {}
        priority_enabled = bool(priority_cfg.get("enabled", True)) and ranking_on
        weight_learning_enabled = bool(priority_cfg.get("weight_learning_enabled", True))
        budget_self_tune_enabled = bool(priority_cfg.get("budget_self_tune_enabled", True))
        state_dir = Path(str(priority_cfg.get("state_dir") or "data/runtime"))
        score_ages_path = state_dir / "universe_score_ages.json"
        weights_path = state_dir / "universe_priority_weights.json"
        budget_path = state_dir / "universe_budget_state.json"
        transitions_path = state_dir / "universe_tier_transitions.json"
        # Curated anchor list — the snapshot fallback already uses these
        # symbols across the dashboard; D118 simply ensures they are pinned
        # into the priority-ranked selection even when their learned
        # liquidity prior is weak.
        from data.universe import UniverseManager as _UM
        from data.universe_builder import _to_yf_symbol as _to_yf

        anchor_symbols: list[str] = []
        for _inst in _UM.INITIAL_UNIVERSE:
            _sym = _to_yf(_inst.broker_symbol or _inst.symbol, _inst.broker)
            if _sym:
                anchor_symbols.append(str(_sym).strip().upper())
        anchor_symbols = list(dict.fromkeys(anchor_symbols))

        first_pipeline_run = True
        forced_discovery_cycle = False
        while True:
            eng = None
            try:
                eng, sf = await init_async_database()
                if sf is not None:
                    if first_pipeline_run:
                        logger.info("orchestrator | pipeline | startup flush — running immediately")
                    elif forced_discovery_cycle:
                        logger.info(
                            "orchestrator | pipeline | forced discovery wake — skipping news and macro feeds"
                        )
                    if universe_mode == "dynamic":
                        if ranking_on:
                            # D117 — resolve adaptive caps for this tick.
                            adaptive_result = None
                            previous_state = load_adaptive_state(adaptive_state_path)
                            if adaptive_on:
                                try:
                                    ctx = await build_adaptive_caps_context(session_factory=sf)
                                except Exception as exc:  # noqa: BLE001
                                    logger.debug(
                                        "orchestrator | adaptive context build failed (non-fatal): {}",
                                        exc,
                                    )
                                    ctx = AdaptiveCapsContext()
                                base = AdaptiveCapsBase(
                                    candidates=baseline_candidates,
                                    watching=baseline_max_symbols,
                                    core=baseline_core_max,
                                    scan=baseline_scan_max,
                                )
                                adaptive_result = compute_adaptive_caps(
                                    base=base, context=ctx, config=adaptive_cfg
                                )
                                universe_builder.update_caps(
                                    max_symbols=adaptive_result.watching,
                                    core_max=adaptive_result.core,
                                    scan_max=adaptive_result.scan,
                                    max_candidates_to_score=adaptive_result.candidates,
                                )
                                logger.info(
                                    "orchestrator | adaptive caps | candidates={} watching={} core={} scan={} mult={:.2f} ({})",
                                    adaptive_result.candidates,
                                    adaptive_result.watching,
                                    adaptive_result.core,
                                    adaptive_result.scan,
                                    adaptive_result.multiplier,
                                    ", ".join(adaptive_result.reasons),
                                )
                            # D118 — priority pre-filter + budget controller.
                            telemetry = BuildTelemetry()
                            priority_scores_in: dict | None = None
                            priority_anchors: list[str] = []
                            budget_for_cycle: int | None = None
                            score_ages_state = None
                            weight_learner: WeightLearner | None = None
                            budget_controller: BudgetController | None = None
                            previous_tiers_snapshot: dict[str, str] = (
                                build_previous_tier_map(
                                    core=list(tiers.core) if "tiers" in dir() else [],
                                    scan=list(tiers.scan) if "tiers" in dir() else [],
                                    light=list(tiers.light) if "tiers" in dir() else [],
                                ) if False else {}  # placeholder; overwritten below
                            )
                            try:
                                from data.universe_tiers import load_universe_tiers as _load_tiers

                                _prev_tiers = _load_tiers()
                                if _prev_tiers is not None:
                                    previous_tiers_snapshot = build_previous_tier_map(
                                        core=list(_prev_tiers.core),
                                        scan=list(_prev_tiers.scan),
                                        light=list(_prev_tiers.light),
                                    )
                            except Exception as exc:  # noqa: BLE001
                                logger.debug(
                                    "orchestrator | D118 prior-tier load failed (non-fatal): {}",
                                    exc,
                                )
                            if priority_enabled:
                                try:
                                    score_ages_state = load_score_ages(score_ages_path)
                                    weight_learner_state = load_weight_learner_state(
                                        weights_path
                                    )
                                    weight_learner = WeightLearner(state=weight_learner_state)
                                    budget_controller = BudgetController(
                                        state=load_budget_state(budget_path)
                                    )
                                    # Build the unique normalized universe by
                                    # calling the broker collector through a
                                    # tiny helper exposed on the builder (the
                                    # actual call inside ``build_tiered_universe``
                                    # uses the same coroutine, so we are not
                                    # paying for two broker scans — the helper
                                    # just returns the dict).
                                    by_broker = await universe_builder._collect_candidates_by_broker(
                                        self._broker_manager
                                    )
                                    unique_norm = list(
                                        dict.fromkeys(
                                            s
                                            for rows in by_broker.values()
                                            for s in rows
                                        )
                                    )
                                    if unique_norm:
                                        # Track newly-seen symbols so the
                                        # learner can give them a freshness
                                        # bonus next cycle.
                                        score_ages_state.observe_unseen(unique_norm)
                                        watching_now = list(previous_tiers_snapshot.keys())
                                        weights_for_cycle = (
                                            weight_learner.current_weights()
                                            if weight_learning_enabled
                                            else {
                                                # Frozen at uniform when the
                                                # learning kill switch is off.
                                                **(weight_learner.current_weights()),
                                            }
                                        )
                                        priority_scores_in = compute_priority_scores(
                                            unique_norm,
                                            score_ages=score_ages_state,
                                            weights=weights_for_cycle,
                                            anchors=anchor_symbols,
                                            watching_now=watching_now,
                                            availability_hints=None,  # registry hints wired below
                                        )
                                        # Compute the next budget BEFORE the
                                        # build so we know how many symbols
                                        # to score.
                                        budget_for_cycle = (
                                            budget_controller.compute_next_budget(
                                                unique_normalized=len(unique_norm),
                                                cycle_interval_sec=float(interval),
                                                concurrency=int(
                                                    ranking_cfg.get("yf_concurrency", 10)
                                                    or 10
                                                ),
                                            )
                                            if budget_self_tune_enabled
                                            else None
                                        )
                                        priority_anchors = [
                                            a for a in anchor_symbols if a in priority_scores_in
                                        ]
                                        logger.info(
                                            "orchestrator | D118 priority pre-filter | unique={} budget={} weights={}",
                                            len(unique_norm),
                                            budget_for_cycle,
                                            {k: round(v, 3) for k, v in weights_for_cycle.items()},
                                        )
                                except Exception as exc:  # noqa: BLE001
                                    logger.warning(
                                        "orchestrator | D118 pre-filter setup failed (non-fatal, falling back to legacy sampler): {}",
                                        exc,
                                    )
                                    priority_scores_in = None
                                    budget_for_cycle = None
                            tiers = await universe_builder.build_tiered_universe(
                                self._broker_manager,
                                priority_scores=priority_scores_in,
                                target_budget=budget_for_cycle,
                                anchors=priority_anchors,
                                telemetry=telemetry,
                            )
                            # D118 — record cycle telemetry into the score-
                            # ages persistence + budget controller +
                            # online weight learner.
                            if priority_enabled and score_ages_state is not None:
                                try:
                                    score_ages_state.record_scores(
                                        dict(tiers.scores),
                                        timeouts=telemetry.timed_out,
                                    )
                                    save_score_ages(
                                        score_ages_state, path=score_ages_path
                                    )
                                except Exception as exc:  # noqa: BLE001
                                    logger.debug(
                                        "orchestrator | D118 score-ages persist failed (non-fatal): {}",
                                        exc,
                                    )
                            if (
                                priority_enabled
                                and weight_learner is not None
                                and weight_learning_enabled
                                and telemetry.picks_breakdowns
                            ):
                                try:
                                    watching_after = list(
                                        set(tiers.core) | set(tiers.scan)
                                    )
                                    rows = build_training_rows(
                                        telemetry.picks_breakdowns,
                                        watching_after,
                                    )
                                    weight_learner.update(rows)
                                    save_weight_learner_state(
                                        weight_learner.state, path=weights_path
                                    )
                                except Exception as exc:  # noqa: BLE001
                                    logger.debug(
                                        "orchestrator | D118 weight-learner update failed (non-fatal): {}",
                                        exc,
                                    )
                            if (
                                priority_enabled
                                and budget_controller is not None
                                and budget_self_tune_enabled
                            ):
                                try:
                                    budget_controller.observe(
                                        CycleObservation(
                                            budget=int(budget_for_cycle or telemetry.picked),
                                            scored=int(telemetry.scored),
                                            measured_duration_sec=float(
                                                telemetry.measured_duration_sec
                                            ),
                                            cycle_interval_sec=float(interval),
                                            concurrency=int(
                                                ranking_cfg.get("yf_concurrency", 10)
                                                or 10
                                            ),
                                            max_watching_rank=telemetry.max_watching_rank,
                                        )
                                    )
                                    save_budget_state(
                                        budget_controller.state, path=budget_path
                                    )
                                except Exception as exc:  # noqa: BLE001
                                    logger.debug(
                                        "orchestrator | D118 budget controller update failed (non-fatal): {}",
                                        exc,
                                    )
                            if priority_enabled and previous_tiers_snapshot:
                                try:
                                    prev_scores: dict[str, float] = {}
                                    try:
                                        _prev_tiers2 = _load_tiers()
                                        if _prev_tiers2 is not None:
                                            prev_scores = dict(_prev_tiers2.scores)
                                    except Exception:
                                        prev_scores = {}
                                    rows = diff_tiers(
                                        previous=previous_tiers_snapshot,
                                        new_core=list(tiers.core),
                                        new_scan=list(tiers.scan),
                                        new_light=list(tiers.light),
                                        scores_previous=prev_scores,
                                        scores_new=dict(tiers.scores),
                                    )
                                    if rows:
                                        record_transitions(rows, path=transitions_path)
                                        logger.info(
                                            "orchestrator | D118 tier transitions recorded | rows={}",
                                            len(rows),
                                        )
                                except Exception as exc:  # noqa: BLE001
                                    logger.debug(
                                        "orchestrator | D118 transitions persist failed (non-fatal): {}",
                                        exc,
                                    )
                            # D117 — anti-churn hysteresis: re-include
                            # symbols that dropped this cycle but were in
                            # the previous watchlist for up to N misses.
                            grace_extended: list[str] = []
                            if adaptive_on and adaptive_result is not None:
                                hysteresis = apply_churn_hysteresis(
                                    new_core=tiers.core,
                                    new_scan=tiers.scan,
                                    new_light=tiers.light,
                                    previous_core=previous_state.context.get("previous_core") or [],
                                    previous_scan=previous_state.context.get("previous_scan") or [],
                                    consecutive_misses=previous_state.consecutive_misses,
                                    policy=adaptive_cfg.churn,
                                )
                                grace_extended = list(hysteresis.grace_extended)
                                if hysteresis.grace_extended:
                                    logger.info(
                                        "orchestrator | adaptive churn | grace_extended={} (kept in scan tier)",
                                        len(hysteresis.grace_extended),
                                    )
                                    tiers = UniverseTiers(
                                        core=hysteresis.core,
                                        scan=hysteresis.scan,
                                        light=hysteresis.light,
                                        scores=tiers.scores,
                                        updated_at=tiers.updated_at,
                                    )
                                    # Persist the hysteresis-adjusted tiers
                                    # so downstream consumers see the same
                                    # watchlist as the dashboard.
                                    tiers_path = ranking_cfg.get("tiers_path")
                                    save_path = None
                                    if isinstance(tiers_path, str) and tiers_path.strip():
                                        save_path = Path(tiers_path.strip())
                                    save_universe_tiers(tiers, path=save_path)
                                next_state = AdaptiveRuntimeState(
                                    enabled=True,
                                    resolved=adaptive_result.as_dict(),
                                    context={
                                        "regime_label": ctx.regime_label,
                                        "breadth_score": ctx.breadth_score,
                                        "signal_pressure": ctx.signal_pressure,
                                        "active_cluster_count": ctx.active_cluster_count,
                                        "note": ctx.note,
                                        "previous_core": list(tiers.core),
                                        "previous_scan": list(tiers.scan),
                                    },
                                    consecutive_misses=hysteresis.consecutive_misses,
                                    last_grace_extended=grace_extended,
                                )
                                try:
                                    save_adaptive_state(next_state, path=adaptive_state_path)
                                except Exception as exc:  # noqa: BLE001
                                    logger.debug(
                                        "orchestrator | adaptive state persist failed (non-fatal): {}",
                                        exc,
                                    )
                            if universe_intel_on:
                                try:
                                    from pathlib import Path

                                    out_path = Path(
                                        str(
                                            (universe_intel_cfg.get("persistence") or {}).get(
                                                "intelligence_json",
                                                "data/runtime/universe_intelligence.json",
                                            )
                                        )
                                    )
                                    intel_result = await build_and_save_universe_intelligence(
                                        tiers,
                                        cfg=universe_intel_cfg,
                                        output_path=out_path,
                                    )
                                    if intel_result.wrote:
                                        logger.info(
                                            "orchestrator | universe intelligence | clusters={} scored={} path={}",
                                            intel_result.clusters,
                                            intel_result.symbols_scored,
                                            intel_result.path,
                                        )
                                    else:
                                        logger.info(
                                            "orchestrator | universe intelligence skipped | reason={}",
                                            intel_result.reason,
                                        )
                                except Exception as exc:  # noqa: BLE001
                                    logger.warning(
                                        "orchestrator | universe intelligence error (non-fatal): {}",
                                        exc,
                                    )
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
                    await run_once(
                        sf,
                        pipeline_cfg,
                        backfill=False,
                        include_news=not forced_discovery_cycle,
                        include_fred=not forced_discovery_cycle,
                    )
                    logger.info(
                        "orchestrator | pipeline cycle complete | first_run={} forced_discovery={}",
                        first_pipeline_run,
                        forced_discovery_cycle,
                    )
                    first_pipeline_run = False
                    forced_discovery_cycle = False
            except Exception as exc:
                logger.warning("orchestrator | pipeline error (non-fatal): {}", exc)
                first_pipeline_run = False  # don't retry-loop on error
                forced_discovery_cycle = False
            finally:
                if eng is not None:
                    try:
                        await _dispose(eng)
                    except Exception:
                        pass
            try:
                forced_discovery_cycle = await self._sleep_cancellable(
                    float(interval),
                    wake_event=self._pipeline_wake_event,
                )
            except asyncio.CancelledError:
                return
