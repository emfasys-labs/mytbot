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
from datetime import datetime, timezone
from collections.abc import Awaitable
from typing import Any

from dotenv import load_dotenv
from loguru import logger

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

        out: dict[str, Any] = {
            "state": self.state.value,
            "state_changed_at": self.state_changed_at.isoformat(),
            "paper_mode": paper_mode,
            "active_brokers": active_brokers,
            "brokers": broker_status,
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
