"""Embedded auto-training scheduler — orchestrator integration smoke tests.

The auto-training scheduler replaced the standalone Windows scheduled task.
These tests cover the timing decision (should we launch the subprocess now?)
without actually shelling out to scripts/auto_train_models.py.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from system.orchestrator import Orchestrator


def _orchestrator() -> Orchestrator:
    Orchestrator._instance = None
    return Orchestrator()


@pytest.mark.asyncio
async def test_tick_skips_when_disabled():
    orc = _orchestrator()
    with patch.object(
        Orchestrator,
        "_resolve_auto_training_config",
        return_value=(False, "03:20", "UTC"),
    ), patch.object(Orchestrator, "_run_auto_training_job", new_callable=AsyncMock) as run:
        await orc._auto_training_tick()
        run.assert_not_awaited()


@pytest.mark.asyncio
async def test_tick_skips_when_subprocess_already_running():
    orc = _orchestrator()
    orc._auto_training_proc_running = True
    with patch.object(
        Orchestrator,
        "_resolve_auto_training_config",
        return_value=(True, "03:20", "UTC"),
    ), patch.object(Orchestrator, "_run_auto_training_job", new_callable=AsyncMock) as run:
        await orc._auto_training_tick()
        run.assert_not_awaited()


@pytest.mark.asyncio
async def test_tick_skips_when_before_scheduled_time():
    """If now-local is 02:00 and schedule is 03:20, do not run."""
    orc = _orchestrator()
    fixed_now = datetime(2026, 5, 21, 2, 0, 0, tzinfo=timezone.utc)
    with patch.object(
        Orchestrator,
        "_resolve_auto_training_config",
        return_value=(True, "03:20", "UTC"),
    ), patch("system.orchestrator.datetime") as dt_mock, patch.object(
        Orchestrator, "_run_auto_training_job", new_callable=AsyncMock
    ) as run:
        dt_mock.now.return_value = fixed_now
        dt_mock.fromisoformat = datetime.fromisoformat
        await orc._auto_training_tick()
        run.assert_not_awaited()


@pytest.mark.asyncio
async def test_tick_runs_when_past_scheduled_time_and_no_prior_run():
    """If now-local is 04:00 and schedule is 03:20 with no prior run, launch."""
    orc = _orchestrator()
    fixed_now = datetime(2026, 5, 21, 4, 0, 0, tzinfo=timezone.utc)
    with patch.object(
        Orchestrator,
        "_resolve_auto_training_config",
        return_value=(True, "03:20", "UTC"),
    ), patch("system.orchestrator.datetime") as dt_mock, patch.object(
        Orchestrator, "_run_auto_training_job", new_callable=AsyncMock
    ) as run:
        dt_mock.now.return_value = fixed_now
        dt_mock.fromisoformat = datetime.fromisoformat
        await orc._auto_training_tick()
        run.assert_awaited_once()


@pytest.mark.asyncio
async def test_tick_skips_when_last_run_is_after_todays_scheduled_time():
    """If we already ran today at 03:21, do not run again at 04:00."""
    orc = _orchestrator()
    fixed_now = datetime(2026, 5, 21, 4, 0, 0, tzinfo=timezone.utc)
    orc._auto_training_last_run_at = datetime(2026, 5, 21, 3, 21, 0, tzinfo=timezone.utc)
    with patch.object(
        Orchestrator,
        "_resolve_auto_training_config",
        return_value=(True, "03:20", "UTC"),
    ), patch("system.orchestrator.datetime") as dt_mock, patch.object(
        Orchestrator, "_run_auto_training_job", new_callable=AsyncMock
    ) as run:
        dt_mock.now.return_value = fixed_now
        dt_mock.fromisoformat = datetime.fromisoformat
        await orc._auto_training_tick()
        run.assert_not_awaited()


@pytest.mark.asyncio
async def test_tick_runs_when_last_run_was_yesterday():
    """If we ran yesterday at 03:21 and it's now 03:25 today, run again."""
    orc = _orchestrator()
    fixed_now = datetime(2026, 5, 21, 3, 25, 0, tzinfo=timezone.utc)
    orc._auto_training_last_run_at = datetime(2026, 5, 20, 3, 21, 0, tzinfo=timezone.utc)
    with patch.object(
        Orchestrator,
        "_resolve_auto_training_config",
        return_value=(True, "03:20", "UTC"),
    ), patch("system.orchestrator.datetime") as dt_mock, patch.object(
        Orchestrator, "_run_auto_training_job", new_callable=AsyncMock
    ) as run:
        dt_mock.now.return_value = fixed_now
        dt_mock.fromisoformat = datetime.fromisoformat
        await orc._auto_training_tick()
        run.assert_awaited_once()


def test_resolve_config_reads_yaml(tmp_path, monkeypatch):
    """The resolver reads config/auto_training.yaml from cwd."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "auto_training.yaml").write_text(
        """
auto_training:
  enabled: true
  timezone: Europe/London
  schedule:
    start_time_local: "03:20"
""",
        encoding="utf-8",
    )
    orc = _orchestrator()
    enabled, start_str, tz = orc._resolve_auto_training_config()
    assert enabled is True
    assert start_str == "03:20"
    assert tz == "Europe/London"


def test_resolve_config_missing_file_returns_none(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    orc = _orchestrator()
    assert orc._resolve_auto_training_config() is None
