"""Tests for Docker compose retry helper."""

from __future__ import annotations

import pytest

from system import dependency_manager as dm


@pytest.mark.asyncio
async def test_start_docker_service_with_retries_succeeds_second_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    async def fake_start(_service: str, _compose_dir: str) -> tuple[bool, str]:
        calls["n"] += 1
        if calls["n"] < 2:
            return False, "registry_busy"
        return True, ""

    async def no_sleep(_s: float) -> None:
        return None

    monkeypatch.setattr(dm, "_start_docker_service", fake_start)
    monkeypatch.setattr(dm.asyncio, "sleep", no_sleep)

    ok, err = await dm._start_docker_service_with_retries("db", "/tmp", attempts=3)
    assert ok is True
    assert err == ""
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_orchestrator_sleep_cancellable_short() -> None:
    from system.orchestrator import Orchestrator

    await Orchestrator._sleep_cancellable(0.6, chunk_sec=0.25)
