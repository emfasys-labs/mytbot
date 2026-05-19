"""Background refresh loops for the D116 instrument registry.

Designed to be started as long-lived asyncio tasks by the orchestrator:

- ``refresh_loop`` runs constituent + availability refresh on configured
  cadences (Wikipedia/iShares daily, broker catalogs hourly).
- ``on_broker_connected`` is invoked by ``system/broker_manager.py`` when a
  broker (re)connects; it schedules a fast availability resolution for the
  fresh broker so previously-unavailable instruments can flip to
  ``available`` immediately.

All work is isolated in ``try/except`` so a scheduler failure cannot break
the trading loop.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Optional

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from instruments.builder import (
    BuilderConfig,
    load_config,
    run_availability,
    run_refresh,
)


DEFAULT_CONSTITUENTS_INTERVAL_SEC = 86_400          # daily
DEFAULT_AVAILABILITY_INTERVAL_SEC = 3_600           # hourly
DEFAULT_OPENFIGI_INTERVAL_SEC = 604_800             # weekly
DEFAULT_STARTUP_DELAY_SEC = 30                       # let the rest of the app come up
MIN_INTERVAL_SEC = 60                                # avoid runaway loops


class RegistryScheduler:
    """Encapsulates the long-running refresh tasks for the registry.

    The orchestrator should call :meth:`start` once at startup. The scheduler
    self-bounds: configuration is reloaded each cycle so YAML edits do not
    require a restart, and a single failure in one loop never affects the
    others.
    """

    def __init__(
        self,
        session_factory_provider,
        broker_manager: Any,
        *,
        constituents_interval_sec: int = DEFAULT_CONSTITUENTS_INTERVAL_SEC,
        availability_interval_sec: int = DEFAULT_AVAILABILITY_INTERVAL_SEC,
        openfigi_interval_sec: int = DEFAULT_OPENFIGI_INTERVAL_SEC,
        startup_delay_sec: int = DEFAULT_STARTUP_DELAY_SEC,
    ) -> None:
        self._session_factory_provider = session_factory_provider
        self._broker_manager = broker_manager
        self._constituents_interval_sec = max(MIN_INTERVAL_SEC, int(constituents_interval_sec))
        self._availability_interval_sec = max(MIN_INTERVAL_SEC, int(availability_interval_sec))
        self._openfigi_interval_sec = max(MIN_INTERVAL_SEC * 5, int(openfigi_interval_sec))
        self._startup_delay_sec = max(0, int(startup_delay_sec))
        self._tasks: list[asyncio.Task] = []
        self._stopping = asyncio.Event()
        self._on_broker_connect_queue: asyncio.Queue[str] = asyncio.Queue(maxsize=64)

    async def _get_session_factory(self) -> Optional[async_sessionmaker[AsyncSession]]:
        try:
            sf = self._session_factory_provider()
            if asyncio.iscoroutine(sf):
                sf = await sf
            return sf
        except Exception as exc:  # noqa: BLE001
            logger.debug("instruments.scheduler | session factory error: {}", exc)
            return None

    async def start(self) -> None:
        if self._tasks:
            return
        logger.info(
            "instruments.scheduler | starting (constituents={}s availability={}s openfigi={}s)",
            self._constituents_interval_sec,
            self._availability_interval_sec,
            self._openfigi_interval_sec,
        )
        self._tasks = [
            asyncio.create_task(self._constituents_loop(), name="instruments-constituents-refresh"),
            asyncio.create_task(self._availability_loop(), name="instruments-availability-refresh"),
            asyncio.create_task(self._openfigi_loop(), name="instruments-openfigi-refresh"),
            asyncio.create_task(self._connect_event_consumer(), name="instruments-connect-events"),
        ]

    async def stop(self) -> None:
        self._stopping.set()
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._tasks = []

    def notify_broker_connected(self, broker_name: str) -> None:
        """Public hook called by ``BrokerManager`` when a broker (re)connects."""
        try:
            self._on_broker_connect_queue.put_nowait(broker_name)
        except asyncio.QueueFull:
            logger.debug(
                "instruments.scheduler | broker_connect queue full, dropping {}", broker_name
            )

    async def _wait_or_stop(self, seconds: int) -> bool:
        try:
            await asyncio.wait_for(self._stopping.wait(), timeout=max(MIN_INTERVAL_SEC, seconds))
        except asyncio.TimeoutError:
            return False
        return True

    async def _constituents_loop(self) -> None:
        await asyncio.sleep(self._startup_delay_sec)
        while not self._stopping.is_set():
            await self._tick_constituents()
            if await self._wait_or_stop(self._constituents_interval_sec):
                return

    async def _availability_loop(self) -> None:
        await asyncio.sleep(self._startup_delay_sec + 5)
        while not self._stopping.is_set():
            await self._tick_availability()
            if await self._wait_or_stop(self._availability_interval_sec):
                return

    async def _openfigi_loop(self) -> None:
        # Stagger from the constituents loop so the heavy weekly pass does not
        # pile up on the daily pass.
        await asyncio.sleep(self._startup_delay_sec + 60)
        while not self._stopping.is_set():
            await self._tick_openfigi()
            if await self._wait_or_stop(self._openfigi_interval_sec):
                return

    async def _connect_event_consumer(self) -> None:
        while not self._stopping.is_set():
            try:
                broker_name = await asyncio.wait_for(
                    self._on_broker_connect_queue.get(), timeout=5
                )
            except asyncio.TimeoutError:
                continue
            await self._handle_broker_connected(broker_name)

    async def _tick_constituents(self) -> None:
        cfg = load_config()
        if not cfg.enabled:
            return
        sf = await self._get_session_factory()
        if sf is None:
            return
        try:
            await run_refresh(
                sf,
                config=cfg,
                broker_manager=self._broker_manager,
                select=None,
                dry_run=False,
                enrich_openfigi=False,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("instruments.scheduler | constituents refresh failed: {}", exc)

    async def _tick_availability(self) -> None:
        cfg = load_config()
        if not cfg.enabled:
            return
        sf = await self._get_session_factory()
        if sf is None or self._broker_manager is None:
            return
        try:
            await run_availability(sf, broker_manager=self._broker_manager, config=cfg)
        except Exception as exc:  # noqa: BLE001
            logger.warning("instruments.scheduler | availability refresh failed: {}", exc)

    async def _tick_openfigi(self) -> None:
        cfg = load_config()
        if not cfg.enabled or not cfg.sources_enabled.get("openfigi", True):
            return
        sf = await self._get_session_factory()
        if sf is None:
            return
        try:
            await run_refresh(
                sf,
                config=cfg,
                broker_manager=self._broker_manager,
                select=("openfigi",),
                dry_run=False,
                enrich_openfigi=True,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("instruments.scheduler | openfigi refresh failed: {}", exc)

    async def _handle_broker_connected(self, broker_name: str) -> None:
        cfg = load_config()
        if not cfg.enabled:
            return
        sf = await self._get_session_factory()
        if sf is None or self._broker_manager is None:
            return
        logger.info(
            "instruments.scheduler | broker_connected={} — running availability resolution",
            broker_name,
        )
        try:
            await run_availability(
                sf,
                broker_manager=self._broker_manager,
                config=cfg,
                only_brokers=[broker_name],
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "instruments.scheduler | availability for {} failed (non-fatal): {}",
                broker_name,
                exc,
            )


def make_scheduler_from_env(
    session_factory_provider,
    broker_manager: Any,
) -> RegistryScheduler:
    """Convenience factory honouring ``INSTRUMENT_REGISTRY_*`` env overrides."""
    return RegistryScheduler(
        session_factory_provider,
        broker_manager,
        constituents_interval_sec=int(
            os.getenv("INSTRUMENT_REGISTRY_CONSTITUENTS_INTERVAL_SEC", DEFAULT_CONSTITUENTS_INTERVAL_SEC)
        ),
        availability_interval_sec=int(
            os.getenv("INSTRUMENT_REGISTRY_AVAILABILITY_INTERVAL_SEC", DEFAULT_AVAILABILITY_INTERVAL_SEC)
        ),
        openfigi_interval_sec=int(
            os.getenv("INSTRUMENT_REGISTRY_OPENFIGI_INTERVAL_SEC", DEFAULT_OPENFIGI_INTERVAL_SEC)
        ),
        startup_delay_sec=int(
            os.getenv("INSTRUMENT_REGISTRY_STARTUP_DELAY_SEC", DEFAULT_STARTUP_DELAY_SEC)
        ),
    )
