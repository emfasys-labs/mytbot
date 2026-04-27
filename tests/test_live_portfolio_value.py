"""Regression tests for :func:`system.portfolio_equity.live_portfolio_value`.

These tests pin the BASE-currency preference that makes IBKR's NetLiquidation
(and similar multi-currency broker reports) aggregate correctly. Without this
preference, naive ``max(balances)`` picks a single cash line and understates
account NAV by the notional of held positions — the exact bug that caused the
UI NAV card to show ~£884k while the true aggregated NAV was ~£1.05M.
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from brokers.base import Balance
from system.portfolio_equity import live_portfolio_snapshot, live_portfolio_value


class _StubAdapter:
    def __init__(self, balances: list[Balance]) -> None:
        self._balances = balances

    async def get_balance(self) -> list[Balance]:
        return list(self._balances)


class _FlakyAdapter:
    def __init__(self, first: list[Balance]) -> None:
        self._first = first
        self._calls = 0

    async def get_balance(self) -> list[Balance]:
        self._calls += 1
        if self._calls == 1:
            return list(self._first)
        raise RuntimeError("temporary balance miss")


class _StubReport:
    def __init__(self, included: list[str]) -> None:
        self._included = included

    @property
    def included_names(self) -> list[str]:
        return list(self._included)


class _StubBrokerManager:
    def __init__(
        self,
        adapters: dict[str, _StubAdapter],
        *,
        included: list[str] | None = None,
    ) -> None:
        self.adapters = adapters
        if included is None:
            included = list(adapters.keys())
        self.report = _StubReport(included)


def _bal(ccy: str, total: str) -> Balance:
    t = Decimal(total)
    return Balance(currency=ccy, total=t, available=t, reserved=Decimal("0"))


@pytest.mark.asyncio
async def test_returns_zero_when_broker_manager_is_none() -> None:
    assert await live_portfolio_value(None) == Decimal(0)


@pytest.mark.asyncio
async def test_returns_zero_when_no_adapters() -> None:
    bm = _StubBrokerManager({})
    assert await live_portfolio_value(bm) == Decimal(0)


@pytest.mark.asyncio
async def test_prefers_base_row_over_larger_cash_row() -> None:
    """IBKR-style: BASE row carries NetLiquidation; non-BASE is just that currency's cash.

    If the helper naively took ``max(balances)`` it would pick the USD cash row
    (884,000) and miss the ~170k of non-cash positions already reflected in
    NetLiquidation (1,055,000).
    """
    ibkr = _StubAdapter(
        [
            _bal("USD", "884000"),
            _bal("GBP", "100"),
            _bal("BASE", "1055000"),
        ]
    )
    bm = _StubBrokerManager({"ibkr": ibkr})
    assert await live_portfolio_value(bm) == Decimal("1055000")


@pytest.mark.asyncio
async def test_prefers_base_even_when_smaller_than_other_rows() -> None:
    """BASE is authoritative even if a non-BASE currency row reports a larger raw total.

    Pins the intent: we trust the broker-reported NAV figure, not the biggest
    number across currency rows.
    """
    ibkr = _StubAdapter(
        [
            _bal("USD", "2000000"),
            _bal("BASE", "1055000"),
        ]
    )
    bm = _StubBrokerManager({"ibkr": ibkr})
    assert await live_portfolio_value(bm) == Decimal("1055000")


@pytest.mark.asyncio
async def test_falls_back_to_max_when_no_base_row() -> None:
    """Crypto venues report per-asset rows without BASE — max preserves the old behaviour."""
    binance = _StubAdapter(
        [
            _bal("USDT", "50000"),
            _bal("BTC", "10000"),
        ]
    )
    bm = _StubBrokerManager({"binance": binance})
    assert await live_portfolio_value(bm) == Decimal("50000")


@pytest.mark.asyncio
async def test_sums_one_row_per_adapter() -> None:
    """Each adapter contributes exactly one equity figure (avoid double-counting)."""
    ibkr = _StubAdapter([_bal("BASE", "1000000"), _bal("USD", "800000")])
    alpaca = _StubAdapter([_bal("USD", "50000")])
    bybit = _StubAdapter([_bal("USDT", "12000"), _bal("BTC", "3000")])
    bm = _StubBrokerManager({"ibkr": ibkr, "alpaca": alpaca, "bybit": bybit})
    assert await live_portfolio_value(bm) == Decimal("1062000")


@pytest.mark.asyncio
async def test_empty_or_zero_balances_are_skipped() -> None:
    empty = _StubAdapter([])
    zero = _StubAdapter([_bal("USD", "0")])
    ok = _StubAdapter([_bal("BASE", "100000")])
    bm = _StubBrokerManager({"kraken": empty, "binance": zero, "ibkr": ok}, included=["ibkr"])
    assert await live_portfolio_value(bm) == Decimal("100000")


@pytest.mark.asyncio
async def test_empty_included_non_ibkr_balance_counts_as_complete_zero() -> None:
    bybit = _StubAdapter([])
    ibkr = _StubAdapter([_bal("BASE", "100000")])
    bm = _StubBrokerManager({"bybit": bybit, "ibkr": ibkr}, included=["bybit", "ibkr"])

    snap = await live_portfolio_snapshot(bm)
    assert snap.complete is True
    assert snap.missing == tuple()
    assert snap.value == Decimal("100000")


@pytest.mark.asyncio
async def test_empty_included_ibkr_balance_suppresses_partial_nav() -> None:
    bybit = _StubAdapter([_bal("USDT", "5000")])
    ibkr = _StubAdapter([])
    bm = _StubBrokerManager({"bybit": bybit, "ibkr": ibkr}, included=["bybit", "ibkr"])

    snap = await live_portfolio_snapshot(bm)
    assert snap.complete is False
    assert snap.missing == ("ibkr",)
    assert snap.value == Decimal("0")


@pytest.mark.asyncio
async def test_adapter_exception_does_not_abort_aggregation() -> None:
    class _Broken:
        async def get_balance(self) -> list[Balance]:
            raise RuntimeError("boom")

    ok = _StubAdapter([_bal("BASE", "100000")])
    bm = _StubBrokerManager({"kraken": _Broken(), "ibkr": ok}, included=["ibkr"])  # type: ignore[dict-item]
    assert await live_portfolio_value(bm) == Decimal("100000")


@pytest.mark.asyncio
async def test_included_adapter_exception_without_cache_suppresses_partial_nav() -> None:
    class _Broken:
        async def get_balance(self) -> list[Balance]:
            raise RuntimeError("boom")

    ok = _StubAdapter([_bal("BASE", "100000")])
    bm = _StubBrokerManager({"kraken": _Broken(), "ibkr": ok}, included=["kraken", "ibkr"])  # type: ignore[dict-item]
    snap = await live_portfolio_snapshot(bm)
    assert snap.value == Decimal("0")
    assert snap.complete is False
    assert snap.missing == ("kraken",)
    assert await live_portfolio_value(bm) == Decimal("0")


@pytest.mark.asyncio
async def test_cached_adapter_value_prevents_transient_partial_nav(monkeypatch: pytest.MonkeyPatch) -> None:
    """A temporary balance miss from an included broker should not publish a partial NAV."""
    monkeypatch.setenv("LIVE_PORTFOLIO_VALUE_CACHE_TTL_SEC", "60")
    ibkr = _FlakyAdapter([_bal("BASE", "1000000")])
    alpaca = _StubAdapter([_bal("USD", "50000")])
    bm = _StubBrokerManager({"ibkr": ibkr, "alpaca": alpaca}, included=["ibkr", "alpaca"])

    assert await live_portfolio_value(bm) == Decimal("1050000")
    assert await live_portfolio_value(bm) == Decimal("1050000")


@pytest.mark.asyncio
async def test_cached_adapter_value_respects_current_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cached values are only used while the broker is still part of live NAV coverage."""
    monkeypatch.setenv("LIVE_PORTFOLIO_VALUE_CACHE_TTL_SEC", "60")
    ibkr = _FlakyAdapter([_bal("BASE", "1000000")])
    alpaca = _StubAdapter([_bal("USD", "50000")])
    bm = _StubBrokerManager({"ibkr": ibkr, "alpaca": alpaca}, included=["ibkr", "alpaca"])

    assert await live_portfolio_value(bm) == Decimal("1050000")
    bm.report = _StubReport(["alpaca"])
    assert await live_portfolio_value(bm) == Decimal("50000")


@pytest.mark.asyncio
async def test_base_currency_case_insensitive() -> None:
    """Some adapters may emit 'base' or 'Base'; helper must still recognise it."""
    ibkr = _StubAdapter([_bal("base", "1055000"), _bal("USD", "884000")])
    bm = _StubBrokerManager({"ibkr": ibkr})
    assert await live_portfolio_value(bm) == Decimal("1055000")


@pytest.mark.asyncio
async def test_excludes_broker_not_in_included_names() -> None:
    """Stale adapters in ``adapters`` must not contribute if not in coverage (D031)."""
    ibkr = _StubAdapter([_bal("BASE", "1000000")])
    alpaca = _StubAdapter([_bal("USD", "50000")])
    bm = _StubBrokerManager({"ibkr": ibkr, "alpaca": alpaca}, included=["alpaca"])
    assert await live_portfolio_value(bm) == Decimal("50000")


@pytest.mark.asyncio
async def test_excludes_risk_engine_disabled() -> None:
    """Risk engine disabled set must be subtracted from the NAV allowlist (D031)."""
    ibkr = _StubAdapter([_bal("BASE", "1000000")])
    alpaca = _StubAdapter([_bal("USD", "50000")])
    bm = _StubBrokerManager({"ibkr": ibkr, "alpaca": alpaca}, included=["ibkr", "alpaca"])
    with patch("system.portfolio_equity.get_risk_engine", return_value=SimpleNamespace(disabled_brokers=frozenset({"ibkr"}))):
        assert await live_portfolio_value(bm) == Decimal("50000")


@pytest.mark.asyncio
async def test_pnl_and_dashboard_agree_on_same_data() -> None:
    """Integration-ish: the /pnl helper (via _live_portfolio_value) must agree with
    the trading loop (live_portfolio_value) on identical inputs.

    This is the whole point of delegating from api.server._live_portfolio_value
    to system.portfolio_equity.live_portfolio_value — prevents the UI NAV card
    from drifting from portfolio.nav again.
    """
    from api import server as api_server

    class _Orch:
        _broker_manager: Any = None

    orch = _Orch()
    ibkr = _StubAdapter([_bal("BASE", "1055000"), _bal("USD", "884000")])
    alpaca = _StubAdapter([_bal("USD", "50000")])
    orch._broker_manager = _StubBrokerManager({"ibkr": ibkr, "alpaca": alpaca})

    original_get = api_server._get_orchestrator
    api_server._get_orchestrator = lambda: orch  # type: ignore[assignment]
    try:
        api_value = await api_server._live_portfolio_value()
    finally:
        api_server._get_orchestrator = original_get  # type: ignore[assignment]

    loop_value = await live_portfolio_value(orch._broker_manager)
    assert api_value == loop_value == Decimal("1105000")
