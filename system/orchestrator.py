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

        self._dep_manager = DependencyManager(compose_dir=os.getcwd())
        self._dep_report: DependencyReport | None = None

        paper_mode = os.getenv("APP_ENV", "paper").strip().lower() != "live"
        self._broker_manager = BrokerManager(paper_mode=paper_mode)
        self._broker_report: BrokerReport | None = None

        self._trading_loop: TradingLoop | None = None
        self._pipeline_task: asyncio.Task | None = None

        self._lock = asyncio.Lock()

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

            try:
                # 1. Infrastructure
                logger.info("orchestrator | ensuring infrastructure...")
                self._dep_report = await self._dep_manager.ensure_all()

                if not self._dep_report.postgres.healthy:
                    err = f"Postgres unavailable: {self._dep_report.postgres.error or 'unknown'}"
                    self.errors.append(err)
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
                )
                await self._trading_loop.start()

                from control.runtime import get_risk_engine
                _re = get_risk_engine()
                if _re is not None and _re.is_killed:
                    _re.reset_kill()
                    logger.info("orchestrator | cleared stale kill switch on fresh start")

                self._set_state(SystemState.RUNNING)
                logger.info(
                    "orchestrator | RUNNING | brokers={} paper={}",
                    active_brokers or "none (observation)",
                    paper_mode,
                )

            except Exception as exc:
                err = f"Startup failed: {exc}"
                self.errors.append(err)
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

            # 1. Stop trading loop
            if self._trading_loop is not None:
                try:
                    await self._trading_loop.stop()
                except Exception as exc:
                    logger.warning("orchestrator | trading loop stop error: {}", exc)
                self._trading_loop = None

            # 2. Cancel pipeline
            if self._pipeline_task is not None and not self._pipeline_task.done():
                self._pipeline_task.cancel()
                try:
                    await self._pipeline_task
                except (asyncio.CancelledError, Exception):
                    pass
                self._pipeline_task = None

            # 3. Cancel all open orders via execution engine kill switch
            if self._trading_loop and self._trading_loop.execution_engine:
                try:
                    await self._trading_loop.execution_engine.cancel_all()
                except Exception as exc:
                    logger.warning("orchestrator | cancel_all error: {}", exc)

            # 4. Disconnect brokers
            try:
                await self._broker_manager.disconnect_all()
            except Exception as exc:
                logger.warning("orchestrator | broker disconnect error: {}", exc)

            self._set_state(SystemState.OFF)
            logger.info("orchestrator | OFF — all components stopped")
            return self.status()

    def status(self) -> dict[str, Any]:
        """Current system status for the API."""
        paper_mode = os.getenv("APP_ENV", "paper").strip().lower() != "live"

        if self._broker_report:
            for name, adapter in self._broker_manager.adapters.items():
                if name in self._broker_report.brokers and not self._broker_report.brokers[name].connected:
                    self._broker_report.brokers[name].connected = True
                    self._broker_report.brokers[name].error = None

        broker_status = self._broker_report.to_dict() if self._broker_report else {}
        active_brokers = self._broker_report.active_names if self._broker_report else []
        dep_status = self._dep_report.to_dict() if self._dep_report else {}

        trading_status = self._trading_loop.status_dict() if self._trading_loop else {
            "running": False, "iterations": 0, "last_iteration_at": None, "last_error": None, "paper_mode": paper_mode,
        }

        return {
            "state": self.state.value,
            "state_changed_at": self.state_changed_at.isoformat(),
            "paper_mode": paper_mode,
            "active_brokers": active_brokers,
            "brokers": broker_status,
            "infrastructure": dep_status,
            "trading": trading_status,
            "errors": list(self.errors),
            "pipeline_running": self._pipeline_task is not None and not self._pipeline_task.done(),
        }

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

        while True:
            eng = None
            try:
                eng, sf = await init_async_database()
                if sf is not None:
                    await run_once(sf, pipeline_cfg, backfill=False)
                    logger.info("orchestrator | pipeline cycle complete")
            except Exception as exc:
                logger.warning("orchestrator | pipeline error (non-fatal): {}", exc)
            finally:
                if eng is not None:
                    try:
                        await _dispose(eng)
                    except Exception:
                        pass
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                return
