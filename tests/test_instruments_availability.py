"""Tests for ``instruments.availability`` resolver (D116)."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from instruments.availability import (
    AvailabilityResolverConfig,
    _resolve_one,
    resolve_broker_availability,
)
from instruments.registry import AvailabilityRow, upsert_broker_availability


class _NoIterationCatalog(set[str]):
    """Broker catalogues must be queried directly, never rebuilt per instrument."""

    def __iter__(self):
        raise AssertionError("catalog was iterated during a single-symbol lookup")
from instruments.registry import RegistryRow


def _row(symbol: str, asset_class: str = "equity") -> RegistryRow:
    now = datetime.now(timezone.utc)
    return RegistryRow(
        canonical_symbol=symbol,
        display_name=symbol,
        asset_class=asset_class,
        region="US",
        exchange=None,
        currency="USD",
        sector=None,
        industry=None,
        isin=None,
        figi=None,
        first_seen_at=now,
        last_seen_at=now,
        last_refreshed_at=now,
        retired_at=None,
    )


def test_resolve_one_alpaca_available_when_in_catalog() -> None:
    res = _resolve_one(
        _row("AAPL"),
        broker="alpaca",
        catalog={"AAPL", "TSLA"},
        ibkr_qualified=set(),
        config=AvailabilityResolverConfig(),
    )
    assert res.status == "available"
    assert res.broker_symbol == "AAPL"


def test_resolve_one_uses_constant_time_catalog_membership() -> None:
    res = _resolve_one(
        _row("AAPL"),
        broker="alpaca",
        catalog=_NoIterationCatalog({"AAPL"}),
        ibkr_qualified=set(),
        config=AvailabilityResolverConfig(),
    )

    assert res.status == "available"


@pytest.mark.asyncio
async def test_availability_persistence_batches_large_registries() -> None:
    class _Context:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    class _Session(_Context):
        def __init__(self) -> None:
            self.statements = []

        def begin(self):
            return _Context()

        async def execute(self, statement):
            self.statements.append(statement)

    session = _Session()
    now = datetime.now(timezone.utc)
    rows = [
        AvailabilityRow(
            canonical_symbol=f"SYM{i}",
            broker="alpaca",
            broker_symbol=f"SYM{i}",
            status="available",
            last_checked_at=now,
            last_available_at=now,
        )
        for i in range(2_501)
    ]
    rows.append(
        AvailabilityRow(
            canonical_symbol="MISSING",
            broker="alpaca",
            broker_symbol="MISSING",
            status="unavailable",
            last_checked_at=now,
            last_available_at=None,
        )
    )

    count = await upsert_broker_availability(
        lambda: session,
        broker="alpaca",
        rows=rows,
    )

    assert count == 2_502
    assert len(session.statements) == 4


def test_resolve_one_alpaca_unavailable_when_missing() -> None:
    res = _resolve_one(
        _row("XYZQ"),
        broker="alpaca",
        catalog={"AAPL", "TSLA"},
        ibkr_qualified=set(),
        config=AvailabilityResolverConfig(),
    )
    assert res.status == "unavailable"


def test_resolve_one_alpaca_unknown_when_catalog_missing() -> None:
    res = _resolve_one(
        _row("AAPL"),
        broker="alpaca",
        catalog=None,
        ibkr_qualified=set(),
        config=AvailabilityResolverConfig(),
    )
    assert res.status == "unknown"


def test_resolve_one_alpaca_international_no_translation() -> None:
    res = _resolve_one(
        _row("HSBA.L"),
        broker="alpaca",
        catalog={"AAPL"},
        ibkr_qualified=set(),
        config=AvailabilityResolverConfig(),
    )
    assert res.status == "unavailable"
    # No broker symbol was constructible
    assert res.broker_symbol is None


def test_resolve_one_ibkr_requires_qualification_when_unknown() -> None:
    res = _resolve_one(
        _row("HSBA.L"),
        broker="ibkr",
        catalog={"SPY"},
        ibkr_qualified=set(),
        config=AvailabilityResolverConfig(),
    )
    assert res.status == "requires_qualification"
    assert res.broker_symbol == "HSBA"  # IBKR uses base ticker; exchange/currency handled at qualification


def test_resolve_one_ibkr_crypto_support_is_whitelisted() -> None:
    res = _resolve_one(
        _row("BTC-USD", asset_class="crypto"),
        broker="ibkr",
        catalog=set(),
        ibkr_qualified=set(),
        config=AvailabilityResolverConfig(),
    )
    assert res.status == "requires_qualification"
    assert res.broker_symbol == "BTC"

    unsupported = _resolve_one(
        _row("AAVE-USD", asset_class="crypto"),
        broker="ibkr",
        catalog=set(),
        ibkr_qualified=set(),
        config=AvailabilityResolverConfig(),
    )
    assert unsupported.status == "unavailable"
    assert unsupported.broker_symbol is None


def test_resolve_one_ibkr_available_when_in_catalog_or_qualified() -> None:
    res = _resolve_one(
        _row("SPY"),
        broker="ibkr",
        catalog={"SPY"},
        ibkr_qualified=set(),
        config=AvailabilityResolverConfig(),
    )
    assert res.status == "available"

    res2 = _resolve_one(
        _row("ROK"),
        broker="ibkr",
        catalog=set(),
        ibkr_qualified={"ROK"},
        config=AvailabilityResolverConfig(),
    )
    assert res2.status == "available"


def test_resolve_one_blocked_overrides_other_states() -> None:
    res = _resolve_one(
        _row("AAPL"),
        broker="alpaca",
        catalog={"AAPL"},
        ibkr_qualified=set(),
        config=AvailabilityResolverConfig(blocked=frozenset({"AAPL"})),
    )
    assert res.status == "blocked"


@pytest.mark.asyncio
async def test_resolve_broker_availability_marks_unknown_when_adapter_none() -> None:
    """Mock the DB layer so the resolver runs end-to-end without Postgres."""

    fake_rows = [_row("AAPL"), _row("HSBA.L")]
    upserted: list[Any] = []

    async def _fake_list_active_registry(*args, **kwargs):
        return fake_rows

    async def _fake_upsert(session_factory, *, broker, rows):
        upserted.append({"broker": broker, "rows": list(rows)})
        return len(rows)

    from instruments import availability as availability_mod

    # Patch dependencies in-place since we cannot use monkeypatch in async context
    saved_list = availability_mod.list_active_registry
    saved_upsert = availability_mod.upsert_broker_availability
    availability_mod.list_active_registry = _fake_list_active_registry  # type: ignore[assignment]
    availability_mod.upsert_broker_availability = _fake_upsert  # type: ignore[assignment]
    try:
        result = await resolve_broker_availability(
            session_factory=SimpleNamespace(),  # unused
            broker="alpaca",
            adapter=None,
        )
    finally:
        availability_mod.list_active_registry = saved_list  # type: ignore[assignment]
        availability_mod.upsert_broker_availability = saved_upsert  # type: ignore[assignment]

    assert result.broker == "alpaca"
    assert result.fetched_catalog is False
    # Symbols that have no broker-side translation are unavailable, the rest are unknown.
    # HSBA.L has no alpaca translation -> unavailable; AAPL has translation but no catalog -> unknown.
    statuses = {r.canonical_symbol: r.status for r in upserted[0]["rows"]}
    assert statuses["HSBA.L"] == "unavailable"
    assert statuses["AAPL"] == "unknown"
