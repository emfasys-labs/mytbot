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
