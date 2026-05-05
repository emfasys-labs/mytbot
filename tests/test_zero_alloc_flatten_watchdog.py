from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from system.orchestrator import Orchestrator, SystemState


@pytest.mark.asyncio
async def test_zero_allocation_watchdog_flattens_when_loop_stale(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "paper")
    monkeypatch.setenv("ZERO_ALLOC_FLATTEN_STALE_SEC", "10")
    monkeypatch.setenv("ZERO_ALLOC_FLATTEN_COOLDOWN_SEC", "30")

    calls: list[dict] = []

    async def fake_flatten(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(count=2)

    monkeypatch.setattr("system.orchestrator.flatten_local_paper_book", fake_flatten)

    orch = Orchestrator()
    orch.state = SystemState.RUNNING
    orch.capital_pct = 0.0
    tl = MagicMock()
    tl.is_running = True
    tl.loop_interval_sec = 120
    tl.last_iteration_at = datetime.now(timezone.utc) - timedelta(seconds=60)
    orch._trading_loop = tl

    await orch._run_zero_alloc_flatten_watchdog_tick()

    assert calls == [{"apply": True, "reason": "zero_allocation_watchdog"}]
    tl.request_iteration.assert_called_once_with("zero_allocation_watchdog_flattened")


@pytest.mark.asyncio
async def test_zero_allocation_watchdog_skips_when_loop_fresh(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "paper")
    monkeypatch.setenv("ZERO_ALLOC_FLATTEN_STALE_SEC", "300")

    async def fake_flatten(**_kwargs):
        raise AssertionError("watchdog should not flatten a fresh loop")

    monkeypatch.setattr("system.orchestrator.flatten_local_paper_book", fake_flatten)

    orch = Orchestrator()
    orch.state = SystemState.RUNNING
    orch.capital_pct = 0.0
    tl = MagicMock()
    tl.is_running = True
    tl.loop_interval_sec = 120
    tl.last_iteration_at = datetime.now(timezone.utc)
    orch._trading_loop = tl

    await orch._run_zero_alloc_flatten_watchdog_tick()

    tl.request_iteration.assert_not_called()


@pytest.mark.asyncio
async def test_zero_allocation_watchdog_refuses_live(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "live")
    monkeypatch.setenv("ZERO_ALLOC_FLATTEN_STALE_SEC", "1")

    async def fake_flatten(**_kwargs):
        raise AssertionError("watchdog must not flatten local paper in live")

    monkeypatch.setattr("system.orchestrator.flatten_local_paper_book", fake_flatten)

    orch = Orchestrator()
    orch.state = SystemState.RUNNING
    orch.capital_pct = 0.0
    tl = MagicMock()
    tl.is_running = True
    tl.last_iteration_at = datetime.now(timezone.utc) - timedelta(seconds=60)
    orch._trading_loop = tl

    await orch._run_zero_alloc_flatten_watchdog_tick()

    tl.request_iteration.assert_not_called()
