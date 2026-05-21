"""
tests/test_forced_discovery.py
==============================

Unit tests for the forced discovery on under-allocation feature.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
import time
from types import SimpleNamespace
import pytest

from data.pipeline import _safe_provider_error, _to_thread_with_retry
from system.orchestrator import Orchestrator
from system.trading_loop.loop import TradingLoop


def test_get_held_cash_used() -> None:
    # Set up a mock portfolio dict with held positions
    portfolio_dict = {
        "positions": {
            "AAPL": {
                "symbol": "AAPL",
                "quantity": "10",
                "current_price": "150.0",  # notional = 1500.0
                "metadata": {"asset_class": "equity"},
            },
            "EURUSD=X": {
                "symbol": "EURUSD=X",
                "quantity": "10000",
                "current_price": "1.1",  # notional = 11000.0
                "metadata": {"asset_class": "forex"},
            },
            "BTC-USD": {
                "symbol": "BTC-USD",
                "quantity": "0.5",
                "current_price": "60000.0",  # notional = 30000.0
                "metadata": {"asset_class": "crypto"},
            },
        }
    }

    # Binding _get_held_cash_used to a SimpleNamespace
    loop_mock = SimpleNamespace(
        _global_edge_cfg={"cash_factors": {"forex": 0.20, "equity": 1.0, "crypto": 1.0}}
    )
    
    cash_used = TradingLoop._get_held_cash_used(loop_mock, portfolio_dict)
    
    # Expected: AAPL = 1500.0 * 1.0 = 1500
    # EURUSD=X = 11000.0 * 0.20 = 2200
    # BTC-USD = 30000.0 * 1.0 = 30000
    # Total = 33700
    assert cash_used == Decimal("33700.0")


@pytest.mark.asyncio
async def test_check_and_trigger_unallocated_capital_discovery_no_wake_event() -> None:
    loop_mock = SimpleNamespace(
        capital_pct=1.0,
        _pipeline_wake_event=None,
    )
    # Should exit early without raising exception
    await TradingLoop._check_and_trigger_unallocated_capital_discovery(
        loop_mock,
        portfolio_dict={"positions": {}},
        total_equity=Decimal("100000.0"),
        executed_count=0,
    )


@pytest.mark.asyncio
async def test_check_and_trigger_unallocated_capital_discovery_zero_capital_pct() -> None:
    event = asyncio.Event()
    loop_mock = SimpleNamespace(
        capital_pct=0.0,
        _pipeline_wake_event=event,
    )
    await TradingLoop._check_and_trigger_unallocated_capital_discovery(
        loop_mock,
        portfolio_dict={"positions": {}},
        total_equity=Decimal("100000.0"),
        executed_count=0,
    )
    assert not event.is_set()


@pytest.mark.asyncio
async def test_check_and_trigger_unallocated_capital_discovery_under_allocated() -> None:
    event = asyncio.Event()
    
    # Target Capital: 100,000 * 1.0 (capital_pct) * 1.0 (gross_fraction) = 100,000
    # Held Cash: 20,000
    # Remaining Cash: 80,000
    # Threshold: 100,000 * 0.05 = 5,000
    # Cooldown: 120s
    loop_mock = SimpleNamespace(
        capital_pct=1.0,
        _pipeline_wake_event=event,
        _global_edge_cfg={},
        loop_interval_sec=120,
        sig_engine=SimpleNamespace(config={"default_position_pct": 0.05}),
        _read_active_mode=lambda: "hunter",
        _get_held_cash_used=lambda p: Decimal("20000.0"),
        _last_unallocated_discovery_trigger_at=0.0,
    )
    
    await TradingLoop._check_and_trigger_unallocated_capital_discovery(
        loop_mock,
        portfolio_dict={"positions": {}},
        total_equity=Decimal("100000.0"),
        executed_count=0,
    )
    
    assert event.is_set()
    assert getattr(loop_mock, "_last_unallocated_discovery_trigger_at") > 0.0


@pytest.mark.asyncio
async def test_check_and_trigger_unallocated_capital_discovery_cooldown() -> None:
    event = asyncio.Event()
    
    # If cooldown has not expired, wake event should NOT be set
    loop_mock = SimpleNamespace(
        capital_pct=1.0,
        _pipeline_wake_event=event,
        _global_edge_cfg={},
        loop_interval_sec=120,
        sig_engine=SimpleNamespace(config={"default_position_pct": 0.05}),
        _read_active_mode=lambda: "hunter",
        _get_held_cash_used=lambda p: Decimal("20000.0"),
        _last_unallocated_discovery_trigger_at=time.monotonic() - 10.0,  # 10s ago, cooldown is 120s
    )
    
    await TradingLoop._check_and_trigger_unallocated_capital_discovery(
        loop_mock,
        portfolio_dict={"positions": {}},
        total_equity=Decimal("100000.0"),
        executed_count=0,
    )
    
    assert not event.is_set()


@pytest.mark.asyncio
async def test_check_and_trigger_unallocated_capital_discovery_fully_allocated() -> None:
    event = asyncio.Event()
    
    # Target Capital: 100,000 * 1.0 (capital_pct) * 1.0 (gross_fraction) = 100,000
    # Held Cash: 98,000
    # Remaining Cash: 2,000
    # Threshold: 100,000 * 0.05 = 5,000 (remains > 1% NAV floor, so 5,000 is used)
    loop_mock = SimpleNamespace(
        capital_pct=1.0,
        _pipeline_wake_event=event,
        _global_edge_cfg={},
        loop_interval_sec=120,
        sig_engine=SimpleNamespace(config={"default_position_pct": 0.05}),
        _read_active_mode=lambda: "hunter",
        _get_held_cash_used=lambda p: Decimal("98000.0"),
        _last_unallocated_discovery_trigger_at=0.0,
    )
    
    await TradingLoop._check_and_trigger_unallocated_capital_discovery(
        loop_mock,
        portfolio_dict={"positions": {}},
        total_equity=Decimal("100000.0"),
        executed_count=0,
    )
    
    assert not event.is_set()


@pytest.mark.asyncio
async def test_check_and_trigger_unallocated_capital_discovery_threshold_clamping() -> None:
    event = asyncio.Event()
    
    # default_position_pct = 0.50 (50% NAV), but gets clamped to the 10% NAV ceiling, so threshold is 10,000.
    # Target Capital: 100,000 * 1.0 = 100,000
    # Held Cash: 92,000
    # Remaining Cash: 8,000 (which is less than the clamped threshold of 10,000, so NO trigger should fire)
    loop_mock = SimpleNamespace(
        capital_pct=1.0,
        _pipeline_wake_event=event,
        _global_edge_cfg={},
        loop_interval_sec=120,
        sig_engine=SimpleNamespace(config={"default_position_pct": 0.50}),
        _read_active_mode=lambda: "hunter",
        _get_held_cash_used=lambda p: Decimal("92000.0"),
        _last_unallocated_discovery_trigger_at=0.0,
    )
    
    await TradingLoop._check_and_trigger_unallocated_capital_discovery(
        loop_mock,
        portfolio_dict={"positions": {}},
        total_equity=Decimal("100000.0"),
        executed_count=0,
    )
    
    assert not event.is_set()


@pytest.mark.asyncio
async def test_sleep_cancellable_reports_forced_wake() -> None:
    event = asyncio.Event()
    event.set()

    woke = await Orchestrator._sleep_cancellable(30.0, wake_event=event)

    assert woke is True
    assert not event.is_set()


@pytest.mark.asyncio
async def test_sleep_cancellable_reports_timeout() -> None:
    woke = await Orchestrator._sleep_cancellable(0.01, chunk_sec=0.01)

    assert woke is False


@pytest.mark.asyncio
async def test_provider_quota_error_is_not_retried() -> None:
    calls = 0

    def fail_quota() -> None:
        nonlocal calls
        calls += 1
        raise RuntimeError("Marketaux error 402 Payment Required: daily request limit reached")

    with pytest.raises(RuntimeError):
        await _to_thread_with_retry(
            fail_quota,
            op_name="marketaux:test",
            attempts=5,
            min_delay_sec=0.01,
            max_delay_sec=0.01,
        )

    assert calls == 1


def test_provider_error_redacts_api_tokens() -> None:
    safe = _safe_provider_error(
        RuntimeError(
            "Client error for url 'https://example.test/news?api_token=secret123&limit=100'; "
            "We have detected your API key as ABC123XYZ"
        )
    )

    assert "secret123" not in safe
    assert "ABC123XYZ" not in safe
    assert "api_token=***" in safe
