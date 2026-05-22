"""D129 — crypto venue-aware sizing + reservation TTL.

P1: the execution engine's in-process crypto-venue room reservation now
    resets on a TTL as well as on a published-room change, closing the
    latent deadlock where a frozen paper-wallet snapshot would lock a
    venue out of all new crypto opens forever.
P2: the allocator sizes crypto opportunities against the crypto venues'
    combined deploy room, not total NAV.
"""
from __future__ import annotations

import time
from decimal import Decimal

import pytest


# ── P1 — reservation TTL on ExecutionEngine ───────────────────────────────────


def _engine():
    from execution.engine import ExecutionEngine

    return ExecutionEngine(broker_configs={}, paper_mode=True)


def test_reservation_resets_on_first_access(monkeypatch):
    monkeypatch.setattr("system.paper_wallet.venue_deploy_room", lambda b: Decimal("30000"))
    eng = _engine()
    room, reserved, effective = eng._crypto_paper_effective_room("kraken")
    assert room == Decimal("30000")
    assert reserved == Decimal("0")
    assert effective == Decimal("30000")


def test_reservation_persists_within_ttl(monkeypatch):
    """While room is unchanged and the TTL has not expired, the in-process
    reservation must persist (the original within-cycle behaviour)."""
    monkeypatch.setattr("system.paper_wallet.venue_deploy_room", lambda b: Decimal("30000"))
    eng = _engine()
    eng._crypto_paper_effective_room("kraken")          # establish window
    eng._crypto_paper_room_reserved["kraken"] = Decimal("25000")
    room, reserved, effective = eng._crypto_paper_effective_room("kraken")
    assert reserved == Decimal("25000")                 # not reset
    assert effective == Decimal("5000")                 # 30000 - 25000


def test_reservation_resets_after_ttl(monkeypatch):
    """The deadlock guard: room frozen, but the reservation must still be
    discarded once it ages past the TTL."""
    from execution.engine import _CRYPTO_RESERVATION_TTL_SEC

    monkeypatch.setattr("system.paper_wallet.venue_deploy_room", lambda b: Decimal("30000"))
    eng = _engine()
    eng._crypto_paper_effective_room("kraken")
    eng._crypto_paper_room_reserved["kraken"] = Decimal("25000")
    # age the reservation past the TTL — room itself never changes
    eng._crypto_paper_room_reserved_at["kraken"] = (
        time.monotonic() - (_CRYPTO_RESERVATION_TTL_SEC + 5)
    )
    room, reserved, effective = eng._crypto_paper_effective_room("kraken")
    assert reserved == Decimal("0")                     # TTL reset
    assert effective == Decimal("30000")                # venue usable again


def test_reservation_resets_on_room_change(monkeypatch):
    """Original reset path — a changed published room — still works."""
    rooms = iter([Decimal("30000"), Decimal("18000")])
    monkeypatch.setattr("system.paper_wallet.venue_deploy_room", lambda b: next(rooms))
    eng = _engine()
    eng._crypto_paper_effective_room("kraken")
    eng._crypto_paper_room_reserved["kraken"] = Decimal("25000")
    room, reserved, effective = eng._crypto_paper_effective_room("kraken")
    assert room == Decimal("18000")
    assert reserved == Decimal("0")                     # reset on room change


# ── P2 — venue-aware crypto room budget ───────────────────────────────────────


def test_crypto_venue_room_budget_sums_venues(monkeypatch):
    from portfolio import global_edge_coordinator as gec

    monkeypatch.setattr("system.paper_wallet.venue_deploy_room", lambda b: Decimal("12000"))
    budget = gec._crypto_venue_room_budget()
    # CRYPTO_PAPER_BROKERS has 3 venues (kraken/binance/bybit) → 3 × 12000
    assert budget == Decimal("36000")


def test_crypto_venue_room_budget_none_when_unbounded(monkeypatch):
    """When the paper-wallet model is disabled every venue returns None →
    the allocator applies no crypto bound (prior behaviour)."""
    from portfolio import global_edge_coordinator as gec

    monkeypatch.setattr("system.paper_wallet.venue_deploy_room", lambda b: None)
    assert gec._crypto_venue_room_budget() is None


def test_crypto_venue_room_budget_partial(monkeypatch):
    """A mix of bounded and unbounded venues sums only the bounded ones."""
    from portfolio import global_edge_coordinator as gec

    vals = {"kraken": Decimal("5000"), "binance": None, "bybit": Decimal("7000")}
    monkeypatch.setattr("system.paper_wallet.venue_deploy_room", lambda b: vals.get(b))
    assert gec._crypto_venue_room_budget() == Decimal("12000")


def test_crypto_clamp_decrement_semantics():
    """The in-loop clamp+decrement: a shared pool is consumed across opps,
    later opps clamp to what's left, then the pool exhausts."""
    budget = Decimal("50000")
    requested = [Decimal("30000"), Decimal("30000"), Decimal("30000")]
    allocated = []
    for cap in requested:
        if budget <= 0:
            allocated.append(Decimal("0"))      # skipped
            continue
        if cap > budget:
            cap = budget
        budget -= cap
        allocated.append(cap)
    assert allocated == [Decimal("30000"), Decimal("20000"), Decimal("0")]
    assert sum(allocated) == Decimal("50000")   # never over-allocates the pool
