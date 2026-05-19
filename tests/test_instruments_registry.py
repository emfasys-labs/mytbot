"""Tests for ``instruments.registry`` pure helpers + persistence layer (D116).

Coverage:
- ``coerce_contribution`` normalises raw rows into ``SourceContribution``s.
- Symbols that cannot be parsed by ``instruments.canonical`` are rejected.
- Asset-class / region hints propagate through.

Full upsert tests against a real Postgres database live separately (the
upsert path uses Postgres-specific ``ON CONFLICT DO UPDATE``).
"""

from __future__ import annotations

import pytest

from instruments.registry import (
    AVAILABILITY_STATES,
    AvailabilityRow,
    RegistryRow,
    SourceContribution,
    coerce_contribution,
)


def test_coerce_contribution_us_equity_minimal() -> None:
    c = coerce_contribution("AAPL", display_name="Apple", region_hint="US")
    assert c is not None
    assert c.canonical_symbol == "AAPL"
    assert c.asset_class == "equity"
    assert c.region == "US"
    assert c.display_name == "Apple"


def test_coerce_contribution_uk_lse_keeps_suffix() -> None:
    c = coerce_contribution("HSBA.L", display_name="HSBC")
    assert c is not None
    assert c.canonical_symbol == "HSBA.L"
    assert c.region == "UK"
    assert c.exchange == "LSE"


def test_coerce_contribution_fx_pair_normalises() -> None:
    c = coerce_contribution("EUR.USD", broker="ibkr", asset_class_hint="fx")
    assert c is not None
    assert c.canonical_symbol == "EURUSD=X"
    assert c.asset_class == "fx"


def test_coerce_contribution_crypto_kraken_normalises() -> None:
    c = coerce_contribution("XBT/USD", broker="kraken", asset_class_hint="crypto")
    assert c is not None
    assert c.canonical_symbol == "BTC-USD"
    assert c.asset_class == "crypto"


def test_coerce_contribution_invalid_returns_none() -> None:
    assert coerce_contribution("") is None
    assert coerce_contribution("$$$") is None
    # symbols with embedded spaces or unsupported punctuation are rejected
    assert coerce_contribution("FOO BAR") is None


def test_coerce_contribution_trims_and_normalises_currency_isin() -> None:
    c = coerce_contribution(
        "SAP.DE",
        currency=" eur ",
        isin=" de0007164600 ",
        figi=" BBG000BVPV84 ",
    )
    assert c is not None
    assert c.currency == "EUR"
    assert c.isin == "DE0007164600"
    assert c.figi == "BBG000BVPV84"


def test_availability_states_enum_subset() -> None:
    assert "available" in AVAILABILITY_STATES
    assert "requires_qualification" in AVAILABILITY_STATES
    assert "blocked" in AVAILABILITY_STATES
    assert "unknown" in AVAILABILITY_STATES
    assert "unavailable" in AVAILABILITY_STATES


def test_registry_row_dataclass_is_immutable() -> None:
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    row = RegistryRow(
        canonical_symbol="AAPL",
        display_name="Apple",
        asset_class="equity",
        region="US",
        exchange=None,
        currency="USD",
        sector=None,
        industry=None,
        isin=None,
        figi=None,
        first_seen_at=now,
        last_seen_at=now,
        last_refreshed_at=None,
        retired_at=None,
    )
    with pytest.raises(Exception):
        row.canonical_symbol = "TSLA"  # type: ignore[misc]


def test_source_contribution_dedupe_friendly() -> None:
    c1 = coerce_contribution("AAPL")
    c2 = coerce_contribution("AAPL")
    assert c1 is not None and c2 is not None
    assert c1.canonical_symbol == c2.canonical_symbol


def test_availability_row_round_trips() -> None:
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    row = AvailabilityRow(
        canonical_symbol="AAPL",
        broker="alpaca",
        broker_symbol="AAPL",
        status="available",
        last_checked_at=now,
        last_available_at=now,
        last_error=None,
    )
    assert row.status == "available"
    assert row.broker == "alpaca"
