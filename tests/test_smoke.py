"""Lightweight import and registry checks (no broker credentials required)."""

from __future__ import annotations

import pytest


def test_broker_registry_lists_expected_names() -> None:
    from brokers.registry import BROKER_REGISTRY

    names = set(BROKER_REGISTRY.keys())
    assert "ibkr" in names
    assert "kraken" in names
    assert "binance" in names
    assert "alpaca" in names


def test_storage_models_base_has_expected_tables() -> None:
    from storage.models import Base

    table_names = set(Base.metadata.tables.keys())
    assert "signals" in table_names
    assert "price_history" in table_names
    assert "orders" in table_names


@pytest.mark.asyncio
async def test_init_async_database_returns_tuple() -> None:
    """Without Postgres, init should return (None, None) without raising."""
    from storage.db import init_async_database

    engine, factory = await init_async_database()
    assert engine is None or factory is not None
    if engine is not None:
        from storage.db import dispose_engine

        await dispose_engine(engine)
