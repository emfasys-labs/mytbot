"""
NAV heartbeat: persist ``daily_pnl`` on a cadence and at shutdown.

Context
-------
Before this feature, ``daily_pnl`` only received a row when a trade filled.
A quiet trading day plus an ungraceful shutdown (OS kill, power loss, crash)
could leave the system with either no row for today or a stale one from
yesterday — the DB fallback in ``/pnl`` therefore had nothing fresh to show
when brokers were slow to report balances post-restart, which is one of the
failure modes that produced the "I lost £200k overnight" report.

Contract pinned here:

* The heartbeat task is started by ``start()`` and cancelled by ``stop()``.
* Every tick calls a single upsert of today's NAV using the live, BASE-aware
  ``system.portfolio_equity.live_portfolio_value`` helper.
* A tick with zero aggregated equity is a **no-op** — we never clobber a
  valid historical row with a spurious zero (e.g. during a transient all-
  brokers-disconnected moment).
* ``stop()`` flushes one final heartbeat before disconnecting brokers, so
  the persisted NAV is up-to-date the instant the process exits.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from system.orchestrator import Orchestrator


class _StubBrokerManager:
    def __init__(self, balances_by_broker: dict[str, list] | None = None) -> None:
        self.adapters: dict = {}
        self.configs: list = []

    async def disconnect_all(self) -> None:  # pragma: no cover - unused in these tests
        return None


@pytest.mark.asyncio
async def test_heartbeat_calls_upsert_with_live_equity(monkeypatch) -> None:
    """Happy path: non-zero equity -> one upsert per tick."""
    orch = Orchestrator()
    orch._broker_manager = _StubBrokerManager()

    upsert_mock = AsyncMock()
    monkeypatch.setattr("run_m3._upsert_daily_pnl", upsert_mock)
    monkeypatch.setattr(
        "run_m3._load_portfolio_state",
        AsyncMock(return_value={"portfolio_value": Decimal("1055000")}),
    )
    monkeypatch.setattr(
        "system.portfolio_equity.live_portfolio_value",
        AsyncMock(return_value=Decimal("1055000")),
    )
    fake_engine = MagicMock()
    monkeypatch.setattr(
        "storage.db.init_async_database",
        AsyncMock(return_value=(fake_engine, MagicMock())),
    )
    monkeypatch.setattr("storage.db.dispose_engine", AsyncMock(return_value=None))

    await orch._flush_nav_heartbeat()

    assert upsert_mock.await_count == 1
    state = upsert_mock.await_args.args[1]
    assert state["portfolio_value"] == Decimal("1055000")


@pytest.mark.asyncio
async def test_heartbeat_skips_upsert_when_equity_is_zero(monkeypatch) -> None:
    """Zero aggregated equity (all brokers down mid-flap) must not overwrite a valid DB row."""
    orch = Orchestrator()
    orch._broker_manager = _StubBrokerManager()

    upsert_mock = AsyncMock()
    monkeypatch.setattr("run_m3._upsert_daily_pnl", upsert_mock)
    monkeypatch.setattr(
        "run_m3._load_portfolio_state",
        AsyncMock(return_value={"portfolio_value": Decimal("0")}),
    )
    monkeypatch.setattr(
        "system.portfolio_equity.live_portfolio_value",
        AsyncMock(return_value=Decimal("0")),
    )
    monkeypatch.setattr(
        "storage.db.init_async_database",
        AsyncMock(return_value=(MagicMock(), MagicMock())),
    )
    monkeypatch.setattr("storage.db.dispose_engine", AsyncMock(return_value=None))

    await orch._flush_nav_heartbeat()

    upsert_mock.assert_not_called()


@pytest.mark.asyncio
async def test_heartbeat_swallows_db_errors(monkeypatch) -> None:
    """Broken DB must never crash the orchestrator — heartbeat is best-effort."""
    orch = Orchestrator()
    orch._broker_manager = _StubBrokerManager()

    monkeypatch.setattr(
        "storage.db.init_async_database",
        AsyncMock(side_effect=RuntimeError("db down")),
    )

    # Should return cleanly, not raise.
    await orch._flush_nav_heartbeat()


@pytest.mark.asyncio
async def test_heartbeat_loop_is_cancellable(monkeypatch) -> None:
    """The background loop must wake from its chunked sleep on cancel()."""
    monkeypatch.setenv("NAV_HEARTBEAT_INTERVAL_SEC", "60")
    monkeypatch.setattr(
        "system.orchestrator.Orchestrator._sleep_cancellable",
        staticmethod(lambda total_sec, **_: asyncio.sleep(0.005)),
    )

    orch = Orchestrator()
    orch._broker_manager = _StubBrokerManager()
    monkeypatch.setattr(orch, "_flush_nav_heartbeat", AsyncMock(return_value=None))

    task = asyncio.create_task(orch._nav_heartbeat_loop())
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_start_nav_heartbeat_loop_is_idempotent() -> None:
    """Multiple starts must not spawn duplicate tasks."""
    orch = Orchestrator()
    orch._broker_manager = _StubBrokerManager()

    orch._start_nav_heartbeat_loop()
    first = orch._nav_heartbeat_task
    orch._start_nav_heartbeat_loop()
    second = orch._nav_heartbeat_task
    try:
        assert first is second
    finally:
        if first is not None:
            first.cancel()
            try:
                await first
            except (asyncio.CancelledError, Exception):
                pass
