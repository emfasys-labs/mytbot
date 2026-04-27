"""Snapshot readiness helper for broker dashboard badges."""

import asyncio
import time
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from brokers.base import Balance
from system.broker_manager import (
    BrokerManager,
    BrokerStatus,
    _balance_poll_mean_ready,
    _balance_rows_mean_ready,
)


def test_balance_rows_mean_ready_empty_not_ready() -> None:
    assert _balance_rows_mean_ready([]) is False


def test_balance_rows_mean_ready_nonempty_currency_ok_even_zero_total() -> None:
    row = Balance(
        currency="USD",
        total=Decimal("0"),
        available=Decimal("0"),
        reserved=Decimal("0"),
    )
    assert _balance_rows_mean_ready([row]) is True


def test_balance_rows_mean_ready_no_currency_ignored() -> None:
    row = Balance(
        currency="",
        total=Decimal("1"),
        available=Decimal("1"),
        reserved=Decimal("0"),
    )
    assert _balance_rows_mean_ready([row]) is False


def test_balance_poll_mean_ready_ibkr_still_requires_rows() -> None:
    assert _balance_poll_mean_ready("ibkr", []) is False


def test_balance_poll_mean_ready_non_ibkr_allows_empty_wallet() -> None:
    assert _balance_poll_mean_ready("bybit", []) is True
    assert _balance_poll_mean_ready("kraken", []) is True


def test_kraken_backoff_is_shorter_than_default() -> None:
    mgr = BrokerManager(paper_mode=True)
    mgr._broker_fail_count["kraken"] = 1
    mgr._broker_fail_count["binance"] = 1
    assert mgr._broker_backoff("kraken") < mgr._broker_backoff("binance")


def test_binance_and_bybit_backoff_are_shorter_than_default() -> None:
    mgr = BrokerManager(paper_mode=True)
    mgr._broker_fail_count["binance"] = 1
    mgr._broker_fail_count["bybit"] = 1
    mgr._broker_fail_count["alpaca"] = 1
    assert mgr._broker_backoff("binance") < mgr._broker_backoff("alpaca")
    assert mgr._broker_backoff("bybit") < mgr._broker_backoff("alpaca")


def test_connect_timeout_scales_for_binance_and_bybit() -> None:
    mgr = BrokerManager(paper_mode=True)
    mgr._broker_fail_count["bybit"] = 2
    mgr._broker_fail_count["binance"] = 2
    mgr._broker_fail_count["alpaca"] = 2
    assert mgr._connect_timeout("bybit") > mgr._connect_timeout("alpaca")
    assert mgr._connect_timeout("binance") > mgr._connect_timeout("alpaca")


def test_kraken_persistent_rate_limit_backoff_jumps_to_minutes() -> None:
    """Three consecutive EAPI:Rate limit exceeded means Kraken punished the key.

    After the streak threshold we should back off for many minutes, not seconds,
    both to let the server-side penalty expire and to avoid retriggering it.
    """
    mgr = BrokerManager(paper_mode=True)
    mgr._broker_fail_count["kraken"] = 3

    mgr._broker_rate_limit_streak["kraken"] = 2
    short = mgr._broker_backoff("kraken")
    assert short < 300, f"streak<3 should keep normal backoff, got {short}s"

    mgr._broker_rate_limit_streak["kraken"] = 3
    persistent = mgr._broker_backoff("kraken")
    assert persistent >= 600, f"streak>=3 must be >=10min, got {persistent}s"
    assert persistent <= 1900, f"cap exceeded, got {persistent}s"


def test_kraken_rate_limit_streak_resets_on_non_rate_limit_failure() -> None:
    """A non-rate-limit failure (e.g. network error) must reset the streak.

    Otherwise we'd get stuck in multi-minute backoffs forever after a flap.
    """
    mgr = BrokerManager(paper_mode=True)
    mgr._broker_rate_limit_streak["kraken"] = 5
    mgr._broker_fail_count["kraken"] = 5

    assert mgr._broker_backoff("kraken") >= 600

    mgr._broker_rate_limit_streak["kraken"] = 0
    assert mgr._broker_backoff("kraken") < 300


def _fake_adapter(
    *, connect_returns: bool, last_error: str | None
) -> Any:
    """Minimal stand-in for a BrokerAdapter to exercise _try_connect branches."""

    async def _connect() -> bool:
        return connect_returns

    return SimpleNamespace(
        connect=_connect,
        disconnect=lambda: asyncio.sleep(0),
        get_balance=lambda: asyncio.sleep(0),
        _last_connect_error=last_error,
    )


@pytest.mark.asyncio
async def test_try_connect_surfaces_persistent_rate_limit_after_streak(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Third consecutive Kraken rate-limit must produce the honest error text."""
    mgr = BrokerManager(paper_mode=True)
    status = BrokerStatus(name="kraken", configured=True)
    mgr.report.brokers["kraken"] = status

    monkeypatch.setattr(
        "system.broker_manager.get_broker",
        lambda name, **kw: _fake_adapter(connect_returns=False, last_error="rate_limit"),
    )

    for _ in range(3):
        await mgr._try_connect("kraken", {}, status, timeout=5.0)

    assert mgr._broker_rate_limit_streak["kraken"] == 3
    assert "persistently blocked" in (status.error or "")
    assert "rotate KRAKEN_API_KEY" in (status.error or "")


@pytest.mark.asyncio
async def test_try_connect_surfaces_invalid_nonce(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mgr = BrokerManager(paper_mode=True)
    status = BrokerStatus(name="kraken", configured=True)
    mgr.report.brokers["kraken"] = status

    monkeypatch.setattr(
        "system.broker_manager.get_broker",
        lambda name, **kw: _fake_adapter(connect_returns=False, last_error="invalid_nonce"),
    )

    await mgr._try_connect("kraken", {}, status, timeout=5.0)

    assert "Invalid nonce" in (status.error or "")
    assert mgr._broker_rate_limit_streak.get("kraken", 0) == 0


@pytest.mark.asyncio
async def test_try_connect_rate_limit_streak_resets_on_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mgr = BrokerManager(paper_mode=True)
    status = BrokerStatus(name="kraken", configured=True)
    mgr.report.brokers["kraken"] = status

    monkeypatch.setattr(
        "system.broker_manager.get_broker",
        lambda name, **kw: _fake_adapter(connect_returns=False, last_error="rate_limit"),
    )
    for _ in range(4):
        await mgr._try_connect("kraken", {}, status, timeout=5.0)
    assert mgr._broker_rate_limit_streak["kraken"] == 4

    async def _noop_balance_ready(*args, **kwargs) -> None:
        return None

    monkeypatch.setattr(mgr, "_mark_balance_ready", _noop_balance_ready)
    monkeypatch.setattr(
        "system.broker_manager.get_broker",
        lambda name, **kw: _fake_adapter(connect_returns=True, last_error=None),
    )
    await mgr._try_connect("kraken", {}, status, timeout=5.0)

    assert status.connected is True
    assert mgr._broker_rate_limit_streak["kraken"] == 0
    assert mgr._broker_fail_count["kraken"] == 0


@pytest.mark.asyncio
async def test_reconnect_loop_ibkr_ready_transition_bypasses_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Launching/restarting TWS must trigger IBKR connect on the next poll.

    A previous full connect failure can put IBKR into a multi-minute backoff.
    The cheap readiness probe is allowed to bypass that backoff only when the
    local API transitions from not-ready to ready.
    """
    mgr = BrokerManager(paper_mode=True)
    mgr._HEALTH_POLL_SEC = 0.01
    mgr._ibkr_fail_count = 5
    mgr._ibkr_last_attempt = time.monotonic()
    mgr._broker_ready_state["ibkr"] = False
    status = BrokerStatus(name="ibkr", configured=True, connected=False)
    mgr.report.brokers["ibkr"] = status

    async def _ready_probe(cfg: dict[str, Any], st: BrokerStatus) -> bool:
        return True

    called = asyncio.Event()

    async def _handle(
        cfg: dict[str, Any],
        st: BrokerStatus,
        *,
        probe_ready: bool | None = None,
    ) -> None:
        assert probe_ready is True
        called.set()

    monkeypatch.setattr(mgr, "_probe_ibkr_ready", _ready_probe)
    monkeypatch.setattr(mgr, "_handle_ibkr", _handle)

    task = asyncio.create_task(mgr._reconnect_loop())
    try:
        await asyncio.wait_for(called.wait(), timeout=0.5)
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


def test_is_rate_limit_error_classifies_temporary_lockout() -> None:
    """Kraken EGeneral:Temporary lockout must route through the persistent-
    throttle path — otherwise reconnect attempts extend the lockout window."""
    from brokers.kraken.adapter import _is_rate_limit_error, _is_account_lockout

    class _FakeErr(Exception):
        pass

    lockout = _FakeErr(
        "The account was temporary locked out.\nDetails: {'error': ['EGeneral:Temporary lockout']}"
    )
    rate_limit = _FakeErr(
        "API rate limit exceeded.\nDetails: {'error': ['EAPI:Rate limit exceeded']}"
    )
    unrelated = _FakeErr("Connection reset by peer")

    assert _is_rate_limit_error(lockout) is True
    assert _is_rate_limit_error(rate_limit) is True
    assert _is_rate_limit_error(unrelated) is False

    assert _is_account_lockout(lockout) is True
    assert _is_account_lockout(rate_limit) is False
    assert _is_account_lockout(unrelated) is False
