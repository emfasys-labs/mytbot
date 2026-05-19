"""Tests for IBKR `get_supported_symbols` consumer (D116).

The IBKR adapter must remain backward-compatible:
- With the registry feature flag OFF (default), it must return at minimum the
  curated YAML seed.
- With the flag ON but the registry empty, it must still return the curated
  YAML seed (no regression).
- With the flag ON and the registry populated, the seed must grow to include
  the registry's available/requires_qualification rows for IBKR.
- Any registry/DB error must fall back silently to the curated seed.
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from brokers.ibkr.adapter import IBKRAdapter


@pytest.mark.asyncio
async def test_supported_symbols_uses_curated_seed_when_flag_off() -> None:
    os.environ.pop("IBKR_SUPPORTED_SYMBOLS_USE_REGISTRY", None)
    adapter = IBKRAdapter()
    with patch.object(IBKRAdapter, "_registry_supported_symbols_enabled", return_value=False):
        symbols = await adapter.get_supported_symbols()
    assert isinstance(symbols, list)
    assert len(symbols) > 0  # curated YAML always present


@pytest.mark.asyncio
async def test_supported_symbols_falls_back_to_seed_when_registry_empty() -> None:
    adapter = IBKRAdapter()

    async def _empty() -> list[str]:
        return []

    with patch.object(IBKRAdapter, "_registry_supported_symbols_enabled", return_value=True), patch.object(
        IBKRAdapter, "_registry_supported_symbols", side_effect=_empty
    ):
        symbols = await adapter.get_supported_symbols()
    # We at least have curated YAML symbols available.
    assert len(symbols) > 0


@pytest.mark.asyncio
async def test_supported_symbols_unions_registry_symbols_when_flag_on() -> None:
    adapter = IBKRAdapter()

    extras = ["FAKE_REGISTRY_ONLY_AAA", "FAKE_REGISTRY_ONLY_BBB"]

    async def _extras() -> list[str]:
        return extras

    with patch.object(IBKRAdapter, "_registry_supported_symbols_enabled", return_value=True), patch.object(
        IBKRAdapter, "_registry_supported_symbols", side_effect=_extras
    ):
        symbols = await adapter.get_supported_symbols()
    upper = {s.upper() for s in symbols}
    assert "FAKE_REGISTRY_ONLY_AAA" in upper
    assert "FAKE_REGISTRY_ONLY_BBB" in upper


@pytest.mark.asyncio
async def test_supported_symbols_silently_recovers_from_registry_error() -> None:
    adapter = IBKRAdapter()

    async def _boom() -> list[str]:
        raise RuntimeError("DB down")

    with patch.object(IBKRAdapter, "_registry_supported_symbols_enabled", return_value=True), patch.object(
        IBKRAdapter, "_registry_supported_symbols", side_effect=_boom
    ):
        # Must not raise; should fall back to curated YAML seed.
        symbols = await adapter.get_supported_symbols()
    assert isinstance(symbols, list)
    assert len(symbols) > 0
