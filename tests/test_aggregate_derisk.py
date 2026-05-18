"""
tests/test_aggregate_derisk.py
===============================
Aggregate unrealised-loss de-risk policy — the "death by many small
losers" guard the per-trade stop structurally misses. Pure unit tests
of the dynamic budget + worst-offender selection.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from risk.aggregate_derisk import (
    PositionLoss,
    aggregate_unrealised,
    derisk_budget,
    derisk_enabled,
    select_derisk_closes,
)


def _p(sym, qty, entry, last, broker="ibkr"):
    return PositionLoss(
        broker=broker,
        symbol=sym,
        quantity=Decimal(str(qty)),
        avg_entry_price=Decimal(str(entry)),
        current_price=Decimal(str(last)),
    )


# ── gating ──────────────────────────────────────────────────────────────


def test_enabled_default_on_off_switch(monkeypatch) -> None:
    monkeypatch.delenv("AGG_UNREALISED_DERISK", raising=False)
    assert derisk_enabled() is True
    monkeypatch.setenv("AGG_UNREALISED_DERISK", "0")
    assert derisk_enabled() is False


# ── unrealised math ─────────────────────────────────────────────────────


def test_position_unrealised_long_short_and_guards() -> None:
    assert _p("A", 100, 10, 9).unrealised == Decimal("-100")   # long, down
    assert _p("B", -100, 10, 11).unrealised == Decimal("-100")  # short, up
    assert _p("C", 100, 10, 12).unrealised == Decimal("200")    # long, up
    assert _p("D", 0, 10, 9).unrealised == Decimal("0")         # flat
    assert _p("E", 100, 0, 9).unrealised == Decimal("0")        # no basis
    assert _p("F", 100, 10, 0).unrealised == Decimal("0")       # no mark


def test_aggregate_sum() -> None:
    ps = [_p("A", 100, 10, 9), _p("B", 100, 10, 12), _p("C", -50, 20, 22)]
    # -100 + 200 + (-100) = 0
    assert aggregate_unrealised(ps) == Decimal("0")


# ── dynamic budget ──────────────────────────────────────────────────────


def test_budget_scales_with_nav_and_vol(monkeypatch) -> None:
    monkeypatch.delenv("AGG_UNREALISED_DERISK_NAV_PCT", raising=False)
    nav = Decimal("1000000")
    # default 0.75% of NAV, no vol → 7500
    assert derisk_budget(nav) == Decimal("7500.000")
    assert derisk_budget(Decimal("0")) == Decimal("0")
    monkeypatch.setenv("AGG_UNREALISED_DERISK_NAV_PCT", "0.01")
    assert derisk_budget(nav) == Decimal("10000.00")
    # vol scaling: ref 0.02; vol 0.04 → 2x but clamped to VOL_MAX (2.0) → 20000
    assert derisk_budget(nav, realised_vol=0.04) == Decimal("20000.0000")
    # calm vol 0.002 → 0.1x but clamped to VOL_MIN (0.6) → 6000
    assert derisk_budget(nav, realised_vol=0.002) == Decimal("6000.0000")


# ── selection policy ────────────────────────────────────────────────────


def test_no_action_when_within_budget() -> None:
    nav = Decimal("1000000")  # budget 7500
    ps = [_p("A", 100, 10, 9), _p("B", 100, 10, 9.5)]  # -100 + -50 = -150
    assert select_derisk_closes(ps, nav) == []


def test_no_action_on_net_gain() -> None:
    nav = Decimal("1000000")
    ps = [_p("A", 100, 10, 5), _p("B", 100, 10, 30)]  # -500 + 2000 = +1500
    assert select_derisk_closes(ps, nav) == []


def test_disabled_returns_empty(monkeypatch) -> None:
    monkeypatch.setenv("AGG_UNREALISED_DERISK", "0")
    nav = Decimal("100000")  # budget 750
    ps = [_p("A", 1000, 10, 1)]  # -9000, way over
    assert select_derisk_closes(ps, nav) == []


def test_closes_worst_losers_until_within_budget(monkeypatch) -> None:
    monkeypatch.delenv("AGG_UNREALISED_DERISK", raising=False)
    monkeypatch.setenv("AGG_UNREALISED_DERISK_NAV_PCT", "0.0075")
    nav = Decimal("1000000")  # budget 7500
    ps = [
        _p("WORST", 1000, 10, 4),   # -6000
        _p("BAD", 1000, 10, 6),     # -4000
        _p("MEH", 1000, 10, 9),     # -1000
        _p("WIN", 1000, 10, 12),    # +2000
    ]
    # total = -6000-4000-1000+2000 = -9000  (> budget 7500)
    chosen = select_derisk_closes(ps, nav, max_actions=5)
    syms = [c.symbol for c in chosen]
    # Closing WORST removes -6000 → projected -3000, now within -7500. Stop.
    assert syms == ["WORST"]


def test_max_actions_caps_per_tick() -> None:
    nav = Decimal("100000")  # budget 750
    ps = [_p(f"L{i}", 100, 10, 1) for i in range(10)]  # each -900, total -9000
    chosen = select_derisk_closes(ps, nav, max_actions=2)
    assert len(chosen) == 2  # bounded; rest handled next tick
    assert all(c.unrealised < 0 for c in chosen)


def test_only_losers_selected() -> None:
    nav = Decimal("100000")  # budget 750
    ps = [_p("WIN", 100, 10, 50), _p("LOSE", 100, 10, 1)]  # +4000, -900
    # net = +3100 → net gain → nothing
    assert select_derisk_closes(ps, nav) == []
    # force a net loss: big loser + small winner
    ps2 = [_p("WIN", 1, 10, 11), _p("LOSE", 1000, 10, 1)]  # +1, -9000
    chosen = select_derisk_closes(ps2, nav, max_actions=5)
    assert [c.symbol for c in chosen] == ["LOSE"]
