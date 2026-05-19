"""Tests for ``instruments.scheduler`` (D116)."""

from __future__ import annotations

import asyncio

import pytest

from instruments.scheduler import RegistryScheduler


@pytest.mark.asyncio
async def test_notify_broker_connected_enqueues_and_consumes(monkeypatch: pytest.MonkeyPatch) -> None:
    """A registered broker-connect event should reach the handler."""

    handled: list[str] = []

    async def _fake_handler(self, broker_name: str) -> None:  # noqa: ARG001
        handled.append(broker_name)

    monkeypatch.setattr(
        RegistryScheduler,
        "_handle_broker_connected",
        _fake_handler,
        raising=True,
    )

    scheduler = RegistryScheduler(
        session_factory_provider=lambda: None,
        broker_manager=None,
        # Short intervals so the test does not have to sleep for long;
        # we only exercise the connect-event consumer here.
        constituents_interval_sec=86400,
        availability_interval_sec=86400,
        openfigi_interval_sec=86400,
        startup_delay_sec=0,
    )
    # Start just the connect-event consumer task; we don't want the heavy
    # constituents/availability tasks to fire during the test.
    consumer_task = asyncio.create_task(scheduler._connect_event_consumer())
    try:
        scheduler.notify_broker_connected("alpaca")
        scheduler.notify_broker_connected("ibkr")
        # Wait briefly for the queue to drain
        for _ in range(20):
            if len(handled) >= 2:
                break
            await asyncio.sleep(0.05)
        assert handled == ["alpaca", "ibkr"]
    finally:
        scheduler._stopping.set()
        consumer_task.cancel()
        try:
            await consumer_task
        except (asyncio.CancelledError, Exception):
            pass


@pytest.mark.asyncio
async def test_session_factory_provider_supports_coroutine() -> None:
    """Provider may be sync or async; both should yield the same factory."""

    async def _async_provider():
        return "sentinel"

    scheduler = RegistryScheduler(_async_provider, broker_manager=None)
    out = await scheduler._get_session_factory()
    assert out == "sentinel"


@pytest.mark.asyncio
async def test_start_and_stop_creates_and_cancels_tasks() -> None:
    scheduler = RegistryScheduler(
        session_factory_provider=lambda: None,
        broker_manager=None,
        startup_delay_sec=0,
    )
    await scheduler.start()
    assert len(scheduler._tasks) == 4
    await scheduler.stop()
    assert scheduler._tasks == []
