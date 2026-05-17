"""
tests/test_market_session.py
=============================
Market-session validity gate — the fix for the weekend/overnight stale-
fill bug that polluted all P&L evidence (Codex finding, verified).

Deterministic: every check passes an explicit UTC ``now`` so the tests
are timezone- and clock-independent.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from core.market_session import is_market_open, market_closed_reason


@pytest.fixture(autouse=True)
def _force_gate_on(monkeypatch):
    """The suite disables the gate by default (conftest) so execution-
    mechanics tests don't depend on the wall clock. This module is the
    gate's own test — force it ON, then let individual tests override."""
    monkeypatch.setenv("MARKET_SESSION_GATE", "1")


def _utc(y, m, d, hh, mm=0) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=timezone.utc)


# ── Crypto: always open ─────────────────────────────────────────────────


@pytest.mark.parametrize("ac", ["crypto", "cryptocurrency", "spot_crypto"])
def test_crypto_is_always_open(ac) -> None:
    # Sunday, the actual bug day.
    assert is_market_open(ac, "ETH-USD", _utc(2026, 5, 17, 3, 0)) is True
    assert is_market_open(ac, "BTC-USD", _utc(2026, 5, 16, 23, 0)) is True


# ── US equity/ETF/option: the core bug ──────────────────────────────────


def test_equity_closed_on_sunday() -> None:
    # 2026-05-17 is a Sunday — IBKR/Alpaca equity fills must be refused.
    assert is_market_open("equity", "AAPL", _utc(2026, 5, 17, 15, 0)) is False
    assert is_market_open("etf", "SPY", _utc(2026, 5, 17, 15, 0)) is False


def test_equity_closed_on_saturday() -> None:
    assert is_market_open("equity", "AAPL", _utc(2026, 5, 16, 15, 0)) is False


def test_equity_open_during_regular_session() -> None:
    # Wed 2026-05-13, 14:30 UTC == 10:30 ET → open.
    assert is_market_open("equity", "AAPL", _utc(2026, 5, 13, 14, 30)) is True


def test_equity_closed_overnight_on_a_weekday() -> None:
    # Wed 2026-05-13, 02:00 UTC == Tue 22:00 ET → closed.
    assert is_market_open("equity", "AAPL", _utc(2026, 5, 13, 2, 0)) is False
    # 13:00 UTC == 09:00 ET → still pre-open (before 09:30).
    assert is_market_open("equity", "AAPL", _utc(2026, 5, 13, 13, 0)) is False
    # 20:30 UTC == 16:30 ET → after the close.
    assert is_market_open("equity", "AAPL", _utc(2026, 5, 13, 20, 30)) is False


def test_equity_closed_on_us_holiday_weekday() -> None:
    # Christmas 2026 falls on a Friday — closed even though it's a weekday
    # and within RTH clock hours.
    assert is_market_open("equity", "AAPL", _utc(2026, 12, 25, 15, 0)) is False


def test_extended_hours_env_widens_window(monkeypatch) -> None:
    monkeypatch.setenv("MARKET_SESSION_EXTENDED", "1")
    # 12:00 UTC == 08:00 ET → inside 04:00–20:00 extended window.
    assert is_market_open("equity", "AAPL", _utc(2026, 5, 13, 12, 0)) is True
    monkeypatch.delenv("MARKET_SESSION_EXTENDED", raising=False)
    assert is_market_open("equity", "AAPL", _utc(2026, 5, 13, 12, 0)) is False


# ── Forex: 24x5, closed weekends ────────────────────────────────────────


def test_forex_closed_saturday_open_weekday() -> None:
    assert is_market_open("forex", "EURUSD", _utc(2026, 5, 16, 12, 0)) is False  # Sat
    assert is_market_open("forex", "EURUSD", _utc(2026, 5, 13, 12, 0)) is True   # Wed
    # Sunday before 21:00 UTC closed; after 21:00 open.
    assert is_market_open("forex", "EURUSD", _utc(2026, 5, 17, 18, 0)) is False
    assert is_market_open("forex", "EURUSD", _utc(2026, 5, 17, 22, 0)) is True
    # Friday after 21:00 UTC → closed for the weekend.
    assert is_market_open("forex", "EURUSD", _utc(2026, 5, 15, 22, 0)) is False


# ── Robustness / philosophy ─────────────────────────────────────────────


def test_unknown_asset_class_fails_open() -> None:
    # We must never block what we can't classify.
    assert is_market_open("weird_thing", "???", _utc(2026, 5, 17, 3, 0)) is True
    assert is_market_open(None, "", _utc(2026, 5, 17, 3, 0)) is True


def test_gate_can_be_disabled(monkeypatch) -> None:
    monkeypatch.setenv("MARKET_SESSION_GATE", "0")
    # Even Sunday equity is "open" when the gate is turned off.
    assert is_market_open("equity", "AAPL", _utc(2026, 5, 17, 3, 0)) is True


def test_reason_string_only_when_closed() -> None:
    assert market_closed_reason("crypto", "BTC-USD", _utc(2026, 5, 17, 3, 0)) is None
    r = market_closed_reason("equity", "AAPL", _utc(2026, 5, 17, 15, 0))
    assert r is not None and r.startswith("market_closed:equity")


def test_enum_like_asset_class_is_accepted() -> None:
    class _AC:
        value = "equity"

    assert is_market_open(_AC(), "AAPL", _utc(2026, 5, 17, 15, 0)) is False
    assert is_market_open(_AC(), "AAPL", _utc(2026, 5, 13, 14, 30)) is True
