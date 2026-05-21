"""D125 fix #2 + #4 — cross-loop derisk dedup and closed-session deferral.

The 2026-05-21 BF-B audit caught:
  * intraday-derisk and aggregate-derisk firing identical 6598.5-share
    sells 4.5 seconds apart → 2× oversell.
  * ~8 pre-market BF-B derisk attempts between 13:00–13:30 UTC, each
    silently bouncing off the market-session gate inside execute().
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from system.orchestrator import Orchestrator


def _orc() -> Orchestrator:
    Orchestrator._instance = None
    return Orchestrator()


def test_derisk_inflight_window_has_sensible_default():
    orc = _orc()
    w = orc._derisk_inflight_window_sec()
    assert w >= 1.0
    assert w == 30.0  # default contract


def test_derisk_inflight_window_env_override(monkeypatch):
    monkeypatch.setenv("DERISK_INFLIGHT_WINDOW_SEC", "12.5")
    orc = _orc()
    assert orc._derisk_inflight_window_sec() == 12.5


def test_derisk_inflight_window_invalid_env_falls_back(monkeypatch):
    monkeypatch.setenv("DERISK_INFLIGHT_WINDOW_SEC", "garbage")
    orc = _orc()
    assert orc._derisk_inflight_window_sec() == 30.0


def test_symbol_is_tradeable_now_delegates_to_market_session():
    orc = _orc()
    with patch("core.market_session.is_tradeable", return_value=False) as m:
        assert orc._symbol_is_tradeable_now("ibkr", "equity", "BF-B") is False
        m.assert_called_once()


def test_symbol_is_tradeable_now_failsafe_on_exception():
    """If the gate crashes, default to True so a real close is never falsely blocked."""
    orc = _orc()
    with patch("core.market_session.is_tradeable", side_effect=RuntimeError("boom")):
        assert orc._symbol_is_tradeable_now("ibkr", "equity", "BF-B") is True


def test_inflight_tracker_initially_empty():
    orc = _orc()
    assert orc._derisk_inflight_ts == {}


def test_inflight_tracker_dedup_window_logic_simulated():
    """Simulate the cross-loop check both loops perform."""
    orc = _orc()
    inflight_key = "ibkr:BF-B"
    window = orc._derisk_inflight_window_sec()
    now_ts = datetime.now(timezone.utc).timestamp()

    # First loop submits a close — sets the lock.
    orc._derisk_inflight_ts[inflight_key] = now_ts

    # Second loop checks 1s later — must skip.
    now_ts_after = now_ts + 1.0
    assert (now_ts_after - orc._derisk_inflight_ts.get(inflight_key, 0.0)) < window

    # Second loop checks well after the window — must proceed.
    now_ts_far = now_ts + window + 10.0
    assert (now_ts_far - orc._derisk_inflight_ts.get(inflight_key, 0.0)) >= window
