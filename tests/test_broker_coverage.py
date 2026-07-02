"""
Broker coverage: honest NAV reporting and risk-engine auto-disable.

Background
----------
``BrokerReport.coverage()`` is the single source of truth for whether the
aggregated NAV visible on the dashboard reflects every wallet the operator
asked for. When a configured broker drops mid-session (or fails to come up
at boot), the orchestrator must surface that as *partial coverage* rather
than silently truncating NAV, and it must prevent new orders from being
routed to the excluded broker until it returns.

This file pins the full contract:

* ``full`` is true iff every configured broker is connected + balance-ready.
* ``excluded`` carries the reason from the broker's last error so the UI
  can render a tooltip without re-deriving it.
* The orchestrator's coverage-sync loop calls ``risk.disable_broker(name)``
  for every excluded broker and ``risk.enable_broker(name)`` the moment it
  returns — idempotent, cancellable, resilient to a missing risk engine.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import MagicMock

import pytest

from system.broker_manager import BrokerReport, BrokerStatus
from system.orchestrator import Orchestrator, SystemState


def _mk_report(*rows: tuple[str, bool, bool, bool, str | None]) -> BrokerReport:
    """Build a BrokerReport from ``(name, configured, connected, balance_ready, error)``."""
    report = BrokerReport()
    for name, configured, connected, balance_ready, error in rows:
        report.brokers[name] = BrokerStatus(
            name=name,
            configured=configured,
            connected=connected,
            balance_ready=balance_ready,
            error=error,
        )
    return report


class TestCoverageShape:
    def test_full_when_every_configured_broker_is_live_and_balance_ready(self) -> None:
        report = _mk_report(
            ("alpaca", True, True, True, None),
            ("binance", True, True, True, None),
            ("kraken", True, True, True, None),
        )
        cov = report.coverage()
        assert cov["full"] is True
        assert sorted(cov["included"]) == ["alpaca", "binance", "kraken"]
        assert cov["excluded"] == []

    def test_partial_when_one_broker_is_disconnected(self) -> None:
        report = _mk_report(
            ("alpaca", True, True, True, None),
            ("ibkr", True, False, False, "Gateway not reachable on 127.0.0.1:7497"),
        )
        cov = report.coverage()
        assert cov["full"] is False
        assert cov["included"] == ["alpaca"]
        assert len(cov["excluded"]) == 1
        excl = cov["excluded"][0]
        assert excl["name"] == "ibkr"
        assert excl["connected"] is False
        assert excl["balance_ready"] is False
        assert "Gateway not reachable" in excl["reason"]

    def test_connected_but_no_balance_snapshot_is_excluded(self) -> None:
        """IBKR can connect before its account summary warms up; balance-ready is the gate."""
        report = _mk_report(
            ("alpaca", True, True, True, None),
            ("ibkr", True, True, False, None),
        )
        cov = report.coverage()
        assert cov["full"] is False
        assert cov["included"] == ["alpaca"]
        assert [e["name"] for e in cov["excluded"]] == ["ibkr"]
        assert cov["excluded"][0]["reason"] == "not ready"  # empty error -> placeholder

    def test_fresh_balance_snapshot_settles_before_full_coverage(self) -> None:
        """A reconnect needs a short dashboard hold after the first balance row."""
        report = _mk_report(
            ("alpaca", True, True, True, None),
            ("ibkr", True, True, True, None),
        )
        report.brokers["ibkr"].balance_ready_since = time.monotonic()

        cov = report.coverage()

        assert cov["full"] is False
        assert cov["included"] == ["alpaca"]
        assert cov["excluded"][0]["name"] == "ibkr"
        assert cov["excluded"][0]["reason"] == "balance snapshot settling"

    def test_unconfigured_brokers_do_not_count_toward_coverage(self) -> None:
        """A broker with no API keys in .env is not part of the NAV contract."""
        report = _mk_report(
            ("alpaca", True, True, True, None),
            ("bybit", False, False, False, "Missing API keys in .env"),
        )
        cov = report.coverage()
        assert cov["full"] is True
        assert cov["configured"] == ["alpaca"]
        assert cov["excluded"] == []

    def test_full_is_false_when_nothing_is_configured(self) -> None:
        """Empty configuration is not "100% coverage" — it's no coverage."""
        report = _mk_report(
            ("alpaca", False, False, False, "Missing API keys in .env"),
        )
        cov = report.coverage()
        assert cov["full"] is False
        assert cov["configured"] == []
        assert cov["excluded"] == []

    def test_status_to_dict_includes_paper_mode(self) -> None:
        report = BrokerReport()
        report.brokers["oanda"] = BrokerStatus(
            name="oanda",
            configured=True,
            connected=True,
            balance_ready=True,
            paper_mode=True,
        )
        report.brokers["alpaca"] = BrokerStatus(
            name="alpaca",
            configured=True,
            connected=True,
            balance_ready=True,
            paper_mode=False,
        )
        payload = report.to_dict()
        assert payload["oanda"]["paper_mode"] is True
        assert payload["alpaca"]["paper_mode"] is False


class TestCoverageSync:
    """Orchestrator keeps the risk engine's disabled_brokers set in sync."""

    @pytest.mark.asyncio
    async def test_excluded_broker_is_disabled_at_risk_engine(
        self, monkeypatch
    ) -> None:
        monkeypatch.setenv("COVERAGE_SYNC_STARTUP_GRACE_SEC", "0")
        await self._assert_excluded_broker_is_disabled()

    async def _assert_excluded_broker_is_disabled(self) -> None:
        orch = Orchestrator()
        orch.state = SystemState.RUNNING
        orch._broker_report = _mk_report(
            ("alpaca", True, True, True, None),
            ("ibkr", True, False, False, "zombie"),
        )
        risk = MagicMock()
        risk.disable_broker = MagicMock()
        risk.enable_broker = MagicMock()

        import control.runtime as rt

        rt.set_risk_engine(risk)
        try:
            task = asyncio.create_task(orch._coverage_sync_loop())
            await asyncio.sleep(0.05)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        finally:
            rt.set_risk_engine(None)

        risk.disable_broker.assert_called_with("ibkr", reason="coverage")
        risk.enable_broker.assert_not_called()

    @pytest.mark.asyncio
    async def test_broker_recovers_then_risk_re_enables(self, monkeypatch) -> None:
        """When an excluded broker comes back fully, sync must re-enable it."""
        # Tight tick so both the disable + enable transitions happen inside a
        # bounded asyncio.sleep() window without slowing the suite.
        monkeypatch.setenv("COVERAGE_SYNC_INTERVAL_SEC", "1")
        monkeypatch.setenv("COVERAGE_SYNC_STARTUP_GRACE_SEC", "0")
        monkeypatch.setattr(
            "system.orchestrator.Orchestrator._sleep_cancellable",
            staticmethod(lambda total_sec, **_: asyncio.sleep(0.01)),
        )

        orch = Orchestrator()
        orch.state = SystemState.RUNNING
        orch._broker_report = _mk_report(
            ("alpaca", True, True, True, None),
            ("ibkr", True, False, False, "zombie"),
        )
        risk = MagicMock()
        risk.disable_broker = MagicMock()
        risk.enable_broker = MagicMock()

        import control.runtime as rt

        rt.set_risk_engine(risk)
        try:
            task = asyncio.create_task(orch._coverage_sync_loop())
            await asyncio.sleep(0.05)
            # IBKR comes fully back.
            orch._broker_report.brokers["ibkr"].connected = True
            orch._broker_report.brokers["ibkr"].balance_ready = True
            orch._broker_report.brokers["ibkr"].error = None
            await asyncio.sleep(0.1)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        finally:
            rt.set_risk_engine(None)

        risk.disable_broker.assert_called_with("ibkr", reason="coverage")
        risk.enable_broker.assert_called_with("ibkr", reason="coverage")

    @pytest.mark.asyncio
    async def test_persisted_coverage_disable_is_cleared_after_restart(
        self, monkeypatch
    ) -> None:
        monkeypatch.setattr(
            "system.orchestrator.Orchestrator._sleep_cancellable",
            staticmethod(lambda total_sec, **_: asyncio.sleep(0.01)),
        )
        orch = Orchestrator()
        orch.state = SystemState.RUNNING
        orch._broker_report = _mk_report(
            ("oanda", True, True, True, None),
        )
        risk = MagicMock()
        risk.disabled_brokers = frozenset({"oanda"})
        risk.broker_disable_reasons.side_effect = (
            lambda name: frozenset({"coverage"}) if name == "oanda" else frozenset()
        )

        import control.runtime as rt

        rt.set_risk_engine(risk)
        try:
            task = asyncio.create_task(orch._coverage_sync_loop())
            await asyncio.sleep(0.05)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        finally:
            rt.set_risk_engine(None)

        risk.enable_broker.assert_called_with("oanda", reason="coverage")

    @pytest.mark.asyncio
    async def test_sync_loop_is_a_noop_when_no_risk_engine(self) -> None:
        """Pre-trading-loop phase must not crash the orchestrator."""
        orch = Orchestrator()
        orch.state = SystemState.STARTING
        orch._broker_report = _mk_report(
            ("ibkr", True, False, False, "still coming up"),
        )
        import control.runtime as rt

        rt.set_risk_engine(None)
        task = asyncio.create_task(orch._coverage_sync_loop())
        await asyncio.sleep(0.02)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


class TestStatusDictIncludesCoverage:
    def test_status_exposes_coverage_block(self) -> None:
        orch = Orchestrator()
        orch._broker_report = _mk_report(
            ("alpaca", True, True, True, None),
            ("ibkr", True, False, False, "Gateway not reachable"),
        )
        out = orch.status()
        assert "coverage" in out
        cov = out["coverage"]
        assert cov["full"] is False
        assert cov["included"] == ["alpaca"]
        assert [e["name"] for e in cov["excluded"]] == ["ibkr"]

    def test_status_coverage_block_exists_even_without_broker_report(self) -> None:
        """Before discover_and_connect runs we still want a well-shaped payload."""
        orch = Orchestrator()
        orch._broker_report = None
        out = orch.status()
        assert out["coverage"] == {
            "full": False,
            "configured": [],
            "included": [],
            "excluded": [],
        }
