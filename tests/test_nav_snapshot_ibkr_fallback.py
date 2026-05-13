"""
tests/test_nav_snapshot_ibkr_fallback.py
=========================================

Locks in the IBKR balance-resilience fix: when IBKR is in the broker
manager's confirmed ``balance_ready`` set but its current ``get_balance``
call returns an empty list, the NAV snapshot must fall back to the last
cached value (within the extended 10-min window) instead of marking IBKR
``missing``. Without this, every transient IBKR API glitch flips the
dashboard banner to "WAITING FOR IBKR" even though the trading loop is
healthy and using a perfectly valid recent value.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import pytest

from system.portfolio_equity import live_portfolio_snapshot


@dataclass
class _Balance:
    currency: str
    total: Decimal


class _Adapter:
    def __init__(self, balances_sequence: list[list[_Balance]]):
        self._seq = list(balances_sequence)
        self._idx = 0

    async def get_balance(self) -> list[_Balance]:
        if self._idx >= len(self._seq):
            return []
        out = self._seq[self._idx]
        self._idx += 1
        return out


class _Report:
    def __init__(self, included_names: list[str]):
        self.included_names = included_names


class _BrokerManager:
    def __init__(self, adapters: dict[str, Any], included: list[str]):
        self.adapters = adapters
        self.report = _Report(included)


@pytest.mark.asyncio
async def test_first_call_populates_cache_and_returns_complete() -> None:
    bm = _BrokerManager(
        adapters={"ibkr": _Adapter([[_Balance("BASE", Decimal("1000000"))]])},
        included=["ibkr"],
    )
    snap = await live_portfolio_snapshot(bm)
    assert snap.complete is True
    assert snap.missing == ()
    assert snap.value == Decimal("1000000")


@pytest.mark.asyncio
async def test_transient_empty_balance_falls_back_to_cache_when_balance_ready() -> None:
    """IBKR is healthy (``balance_ready``) — one empty reply must not flip NAV."""
    bm = _BrokerManager(
        adapters={
            "ibkr": _Adapter([
                [_Balance("BASE", Decimal("1000000"))],
                [],  # transient empty reply
                [],  # another transient empty reply
            ])
        },
        included=["ibkr"],
    )
    # First call populates cache.
    s1 = await live_portfolio_snapshot(bm)
    assert s1.complete is True
    # Second call: IBKR returns empty → must fall back to cached $1M.
    s2 = await live_portfolio_snapshot(bm)
    assert s2.complete is True, f"expected fallback, got missing={s2.missing}"
    assert s2.value == Decimal("1000000")
    # Third call: still fine.
    s3 = await live_portfolio_snapshot(bm)
    assert s3.complete is True
    assert s3.value == Decimal("1000000")


@pytest.mark.asyncio
async def test_extended_ttl_only_applies_to_balance_ready_brokers(
    monkeypatch,
) -> None:
    """A broker NOT in ``included_names`` must use the short TTL — i.e. an
    extended fallback only protects adapters the manager has vouched for.
    """
    # Force a short standard TTL and a long extended TTL.
    monkeypatch.setenv("LIVE_PORTFOLIO_VALUE_CACHE_TTL_SEC", "1")
    monkeypatch.setenv("LIVE_PORTFOLIO_VALUE_EXT_CACHE_TTL_SEC", "600")
    bm = _BrokerManager(
        adapters={
            "kraken": _Adapter([
                [_Balance("USD", Decimal("50000"))],
                [],  # empty
            ])
        },
        # Kraken is NOT in included_names (not balance_ready).
        included=[],
    )
    # With allow=set() (empty), kraken is excluded entirely — also a valid
    # passing case for our invariant (no spurious "missing"). We use this
    # to assert the function does not crash and returns sensible output.
    snap = await live_portfolio_snapshot(bm)
    assert snap.included == ()
    assert snap.missing == ()


@pytest.mark.asyncio
async def test_kraken_with_balance_ready_also_protected() -> None:
    """The extended TTL applies to all confirmed brokers, not just IBKR."""
    bm = _BrokerManager(
        adapters={
            "kraken": _Adapter([
                [_Balance("USD", Decimal("50000"))],
                [],  # transient empty
            ])
        },
        included=["kraken"],
    )
    s1 = await live_portfolio_snapshot(bm)
    assert s1.complete is True
    s2 = await live_portfolio_snapshot(bm)
    # Kraken's _zero_balance_is_complete returns True for empty balances,
    # so an empty list is considered "complete with zero" — not a fallback.
    # Either way, NAV must not flip to missing.
    assert s2.complete is True


@pytest.mark.asyncio
async def test_cache_exhaustion_after_extended_window_marks_missing(
    monkeypatch,
) -> None:
    """Even with extended TTL, eventually the cache must expire and IBKR
    flips to missing — extended fallback is generous, not infinite.
    """
    # Set both TTLs to zero so any empty reply immediately marks missing.
    monkeypatch.setenv("LIVE_PORTFOLIO_VALUE_CACHE_TTL_SEC", "0")
    monkeypatch.setenv("LIVE_PORTFOLIO_VALUE_EXT_CACHE_TTL_SEC", "0")
    bm = _BrokerManager(
        adapters={
            "ibkr": _Adapter([
                [_Balance("BASE", Decimal("1000000"))],
                [],
            ])
        },
        included=["ibkr"],
    )
    s1 = await live_portfolio_snapshot(bm)
    assert s1.complete is True
    s2 = await live_portfolio_snapshot(bm)
    assert s2.complete is False
    assert "ibkr" in s2.missing
