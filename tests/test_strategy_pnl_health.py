"""Tests for system/strategy_pnl_health.py (D231 P1.5/P2 — opening_strategy attribution)."""
from __future__ import annotations

from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from storage.fills_ledger import record_fill
from storage.models import Base
from system.strategy_pnl_health import fetch_strategy_pnl_recent, reset_cache


@pytest_asyncio.fixture
async def sf():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    reset_cache()
    yield maker
    await engine.dispose()


@pytest.mark.asyncio
async def test_groups_by_opening_strategy_not_exit_mechanism(sf):
    # Opened by mean_reversion, closed by stop_loss_monitor — the exit
    # mechanism's own name must NOT appear as a key; mean_reversion must.
    await record_fill(sf, broker="ibkr", symbol="MSFT", side="buy",
                      quantity=Decimal("100"), fill_price=Decimal("100"),
                      strategy="mean_reversion", signal_id="sig-1")
    await record_fill(sf, broker="ibkr", symbol="MSFT", side="sell",
                      quantity=Decimal("100"), fill_price=Decimal("90"),
                      strategy="stop_loss_monitor", signal_id="stoploss-1")

    out = await fetch_strategy_pnl_recent(sf)

    assert "mean_reversion" in out
    assert "stop_loss_monitor" not in out
    assert out["mean_reversion"]["net_pnl"] == Decimal("-1000")
    assert out["mean_reversion"]["fills"] == 1


@pytest.mark.asyncio
async def test_excludes_pre_migration_fills_with_null_opening_strategy(sf):
    # Simulate a pre-D231 fill: opening_strategy is NULL even though
    # strategy/realised_pnl are populated.
    async with sf() as session:
        from datetime import datetime, timezone
        from storage.models import FillLog
        session.add(
            FillLog(
                timestamp=datetime.now(timezone.utc),
                broker="ibkr", symbol="AAPL", asset_class="equity", side="sell",
                order_type="market", quantity=Decimal("10"), signed_quantity=Decimal("-10"),
                fill_price=Decimal("90"), notional=Decimal("900"), fee=Decimal("1"),
                realised_pnl=Decimal("-100"), avg_cost_basis=Decimal("100"),
                position_qty_after=Decimal("0"), strategy="stop_loss_monitor",
                opening_strategy=None, is_paper=True,
            )
        )
        await session.commit()

    out = await fetch_strategy_pnl_recent(sf)
    assert out == {}


@pytest.mark.asyncio
async def test_profit_factor_computed_from_wins_and_losses(sf):
    await record_fill(sf, broker="ibkr", symbol="NVDA", side="buy",
                      quantity=Decimal("100"), fill_price=Decimal("50"),
                      strategy="trend_breakout", signal_id="sig-1")
    # Close 40 for a win, reopen isn't needed — partial closes still count.
    await record_fill(sf, broker="ibkr", symbol="NVDA", side="sell",
                      quantity=Decimal("40"), fill_price=Decimal("60"),
                      strategy="profit_harvest_monitor", signal_id="pf-1")
    await record_fill(sf, broker="ibkr", symbol="NVDA", side="sell",
                      quantity=Decimal("60"), fill_price=Decimal("45"),
                      strategy="stop_loss_monitor", signal_id="stoploss-1")

    out = await fetch_strategy_pnl_recent(sf)

    # win = (60-50)*40 = 400; loss = (45-50)*60 = -300 -> pf = 400/300 = 1.333...
    assert out["trend_breakout"]["fills"] == 2
    assert out["trend_breakout"]["profit_factor"] == pytest.approx(400 / 300)


@pytest.mark.asyncio
async def test_profit_factor_infinite_when_only_wins(sf):
    await record_fill(sf, broker="ibkr", symbol="NVDA", side="buy",
                      quantity=Decimal("100"), fill_price=Decimal("50"),
                      strategy="trend_breakout", signal_id="sig-1")
    await record_fill(sf, broker="ibkr", symbol="NVDA", side="sell",
                      quantity=Decimal("100"), fill_price=Decimal("60"),
                      strategy="profit_harvest_monitor", signal_id="pf-1")

    out = await fetch_strategy_pnl_recent(sf)
    assert out["trend_breakout"]["profit_factor"] == float("inf")
