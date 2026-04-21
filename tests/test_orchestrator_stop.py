"""Orchestrator shutdown: order of operations and runtime registry cleanup."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from control.runtime import get_execution_engine, get_risk_engine, set_execution_engine, set_risk_engine
from system.orchestrator import Orchestrator, SystemState


@pytest.mark.asyncio
async def test_stop_runs_cancel_all_while_engine_alive_then_disconnects() -> None:
    orch = Orchestrator()
    orch.state = SystemState.RUNNING

    bm = MagicMock()
    bm.disconnect_all = AsyncMock()
    orch._broker_manager = bm

    ee_cancel = AsyncMock()
    ee = MagicMock()
    ee.cancel_all = ee_cancel
    tl = MagicMock()
    tl.stop = AsyncMock()
    tl.execution_engine = ee
    orch._trading_loop = tl
    orch._pipeline_task = None

    await orch.stop()

    tl.stop.assert_awaited_once()
    ee_cancel.assert_awaited_once()
    bm.disconnect_all.assert_awaited_once()
    assert orch.state == SystemState.OFF
    assert orch._trading_loop is None


@pytest.mark.asyncio
async def test_stop_clears_risk_and_execution_globals() -> None:
    set_risk_engine(MagicMock(is_killed=False))
    set_execution_engine(MagicMock())

    orch = Orchestrator()
    orch.state = SystemState.RUNNING
    orch._broker_manager = MagicMock()
    orch._broker_manager.disconnect_all = AsyncMock()
    tl = MagicMock()
    tl.stop = AsyncMock()
    tl.execution_engine = MagicMock()
    tl.execution_engine.cancel_all = AsyncMock()
    orch._trading_loop = tl
    orch._pipeline_task = None

    await orch.stop()

    assert get_risk_engine() is None
    assert get_execution_engine() is None


@pytest.mark.asyncio
async def test_stop_reaches_off_when_cancel_all_exceeds_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """Broker cancel_all must not strand STOPPING if adapters block on I/O."""
    monkeypatch.setenv("ORCHESTRATOR_STOP_CANCEL_ALL_SEC", "0.05")

    orch = Orchestrator()
    orch.state = SystemState.RUNNING

    bm = MagicMock()
    bm.disconnect_all = AsyncMock()
    orch._broker_manager = bm

    async def slow_cancel() -> None:
        await asyncio.sleep(30.0)

    ee = MagicMock()
    ee.cancel_all = slow_cancel
    tl = MagicMock()
    tl.stop = AsyncMock()
    tl.execution_engine = ee
    orch._trading_loop = tl
    orch._pipeline_task = None

    await orch.stop()

    assert orch.state == SystemState.OFF
    bm.disconnect_all.assert_awaited_once()
