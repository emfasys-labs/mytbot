"""
tests/test_orchestrator_post_fill_persist.py
=============================================

Locks in the fix for the phantom-close loop: when the profit-harvest or
stop-loss monitor fires a paper close, the position must be persisted to
PositionLog so the monitor doesn't see the same "still profitable"
position next tick and fire another redundant close. Observed in
production: 19 BRTX buys in 30 min, all filled, no position change.
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from system.orchestrator import Orchestrator


@pytest.mark.asyncio
async def test_persist_fill_calls_apply_and_persist_when_filled() -> None:
    """Filled close → must invoke _apply_signal_to_portfolio_state +
    _persist_position_snapshot + _upsert_daily_pnl."""
    orch = Orchestrator()
    signal = SimpleNamespace(
        symbol="BRTX",
        side="buy",
        suggested_quantity=Decimal("5069.57"),
        suggested_price=Decimal("0.187"),
        broker="alpaca",
        asset_class="equity",
        metadata={"reduce_only": True},
    )
    result = SimpleNamespace(
        filled_quantity=Decimal("5069.57"),
        avg_fill_price=Decimal("0.1874"),
        fee=Decimal("0.05"),
    )

    state_after_load = {"positions": {}, "high_watermark_value": Decimal("100000")}
    with patch("run_m3._load_portfolio_state", new=AsyncMock(return_value=state_after_load)) as p_load, \
         patch("run_m3._apply_signal_to_portfolio_state") as p_apply, \
         patch("run_m3._persist_position_snapshot", new=AsyncMock()) as p_persist, \
         patch("run_m3._upsert_daily_pnl", new=AsyncMock()) as p_upsert:
        await orch._persist_fill_to_portfolio_state(
            sf=object(),  # opaque — only passed through
            signal=signal,
            result=result,
            fallback_nav=Decimal("100000"),
        )

    p_load.assert_awaited_once()
    p_apply.assert_called_once()
    p_persist.assert_awaited_once()
    p_upsert.assert_awaited_once()


@pytest.mark.asyncio
async def test_persist_fill_updates_signal_fill_fields() -> None:
    """The signal's quantity/price must reflect the actual fill, not the original intent."""
    orch = Orchestrator()
    signal = SimpleNamespace(
        symbol="BRTX",
        side="buy",
        suggested_quantity=Decimal("5070"),  # intent
        suggested_price=Decimal("0.187"),    # intent
        broker="alpaca",
        asset_class="equity",
        metadata={"reduce_only": True},
    )
    result = SimpleNamespace(
        filled_quantity=Decimal("5069.57"),  # actual
        avg_fill_price=Decimal("0.1874"),    # actual
        fee=Decimal("0.05"),
    )

    with patch("run_m3._load_portfolio_state", new=AsyncMock(return_value={"positions": {}})), \
         patch("run_m3._apply_signal_to_portfolio_state"), \
         patch("run_m3._persist_position_snapshot", new=AsyncMock()), \
         patch("run_m3._upsert_daily_pnl", new=AsyncMock()):
        await orch._persist_fill_to_portfolio_state(
            sf=object(), signal=signal, result=result, fallback_nav=Decimal("0"),
        )

    assert signal.suggested_quantity == Decimal("5069.57")
    assert signal.suggested_price == Decimal("0.1874")


@pytest.mark.asyncio
async def test_persist_fill_skips_when_zero_filled_quantity() -> None:
    """Zero-fill (rejected/cancelled) must not run persistence — no position change."""
    orch = Orchestrator()
    signal = SimpleNamespace(
        symbol="BRTX", side="buy", suggested_quantity=Decimal("0"),
        suggested_price=Decimal("0"), broker="alpaca", asset_class="equity",
        metadata={},
    )
    result = SimpleNamespace(filled_quantity=Decimal("0"), avg_fill_price=None, fee=Decimal("0"))

    with patch("run_m3._load_portfolio_state", new=AsyncMock()) as p_load, \
         patch("run_m3._persist_position_snapshot", new=AsyncMock()) as p_persist, \
         patch("run_m3._upsert_daily_pnl", new=AsyncMock()) as p_upsert:
        await orch._persist_fill_to_portfolio_state(
            sf=object(), signal=signal, result=result, fallback_nav=Decimal("0"),
        )

    # Zero-fill: no DB writes at all.
    p_load.assert_not_awaited()
    p_persist.assert_not_awaited()
    p_upsert.assert_not_awaited()


@pytest.mark.asyncio
async def test_persist_fill_swallows_exceptions() -> None:
    """A failure inside persistence must NOT break the orchestrator loop."""
    orch = Orchestrator()
    signal = SimpleNamespace(
        symbol="X", side="buy", suggested_quantity=Decimal("1"),
        suggested_price=Decimal("1"), broker="ibkr", asset_class="equity",
        metadata={},
    )
    result = SimpleNamespace(filled_quantity=Decimal("1"), avg_fill_price=Decimal("1"), fee=Decimal("0"))

    boom = AsyncMock(side_effect=RuntimeError("db down"))
    with patch("run_m3._load_portfolio_state", new=boom):
        # Must not raise — the orchestrator continues even if persist fails.
        await orch._persist_fill_to_portfolio_state(
            sf=object(), signal=signal, result=result, fallback_nav=Decimal("0"),
        )
