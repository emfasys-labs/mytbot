from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from storage.models import Base, FillLog, PositionLog
from system.runtime_invariants import audit_runtime_invariants


@pytest.mark.asyncio
async def test_runtime_invariants_detects_fill_position_mismatch() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sf = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime.now(timezone.utc)
    async with sf() as session:
        session.add(
            FillLog(
                timestamp=now,
                broker="kraken",
                symbol="BTC-USD",
                asset_class="crypto",
                side="buy",
                order_type="market",
                quantity=Decimal("1"),
                signed_quantity=Decimal("1"),
                fill_price=Decimal("100"),
                notional=Decimal("100"),
                fee=Decimal("0"),
                realised_pnl=Decimal("0"),
                position_qty_after=Decimal("1"),
            )
        )
        session.add(
            PositionLog(
                timestamp=now,
                broker="kraken",
                symbol="BTC-USD",
                quantity=Decimal("0.5"),
                avg_entry_price=Decimal("100"),
                current_price=Decimal("100"),
                unrealised_pnl=Decimal("0"),
                asset_class="crypto",
            )
        )
        await session.commit()

    report = await audit_runtime_invariants(sf, stale_order_seconds=1200)

    assert report["healthy"] is False
    assert report["fill_position_mismatches"][0]["symbol"] == "BTC-USD"
    await engine.dispose()


@pytest.mark.asyncio
async def test_runtime_invariants_detects_economic_duplicates_and_cash_alpha() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sf = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime.now(timezone.utc)
    rows = [
        ("alpaca", "CME", "equity", Decimal("10"), Decimal("200")),
        ("capitalcom", "CME", "equity", Decimal("1"), Decimal("200")),
        ("binance", "FIDD-USD", "crypto", Decimal("5000"), Decimal("1")),
    ]
    async with sf() as session:
        for broker, symbol, asset_class, quantity, price in rows:
            session.add(
                FillLog(
                    timestamp=now,
                    broker=broker,
                    symbol=symbol,
                    asset_class=asset_class,
                    side="buy",
                    order_type="market",
                    quantity=quantity,
                    signed_quantity=quantity,
                    fill_price=price,
                    notional=quantity * price,
                    fee=Decimal("0"),
                    realised_pnl=Decimal("0"),
                    position_qty_after=quantity,
                )
            )
            session.add(
                PositionLog(
                    timestamp=now,
                    broker=broker,
                    symbol=symbol,
                    quantity=quantity,
                    avg_entry_price=price,
                    current_price=price,
                    unrealised_pnl=Decimal("0"),
                    asset_class=asset_class,
                )
            )
        await session.commit()

    report = await audit_runtime_invariants(sf, stale_order_seconds=1200)

    assert report["healthy"] is False
    assert report["duplicate_economic_positions"][0]["economic_symbol"] == "CME"
    assert report["cash_equivalent_alpha_positions"][0]["symbol"] == "FIDD-USD"
    assert report["legacy_reconciliation_plan"]
    await engine.dispose()
