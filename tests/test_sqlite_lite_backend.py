"""
M11 Lite profile — SQLite backend correctness.

The Docker-free Lite profile runs the same schema on SQLite. SQLAlchemy's bare
``Numeric`` corrupts Decimal on SQLite (see ``scripts/spike_sqlite_decimal.py``),
so money columns use ``storage.types.DecimalSafe`` (TEXT-backed on SQLite,
native NUMERIC on Postgres) and the position-critical ``SUM(signed_quantity)`` is
summed in Python on SQLite.

These tests pin that behaviour on a real in-memory SQLite engine:
  1. The full schema builds via ``create_all`` on SQLite.
  2. Decimal columns round-trip exactly (the values float would corrupt).
  3. The race-free position quantity is exact (not double-precision SUM).
  4. The open-position ``abs(quantity) > eps`` filter behaves correctly.
"""
from __future__ import annotations

from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from storage.fills_ledger import available_quantity, record_fill
from storage.models import Base, FillLog, PositionLog
from datetime import datetime, timezone


@pytest_asyncio.fixture
async def sf():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)  # (1) schema builds on SQLite
    maker = async_sessionmaker(engine, expire_on_commit=False)
    yield maker
    await engine.dispose()


# Values that bare Numeric(REAL) corrupts on SQLite (per the spike).
_EXACT_CASES = [
    Decimal("70123.45"),
    Decimal("0.00000001"),
    Decimal("9999999.99999999"),
    Decimal("0.10000000"),
]


@pytest.mark.asyncio
async def test_decimalsafe_exact_roundtrip(sf):
    async with sf() as session:
        for i, v in enumerate(_EXACT_CASES):
            session.add(FillLog(
                id=i + 1,
                timestamp=datetime.now(timezone.utc),
                broker="ibkr", symbol="AAPL", side="buy",
                quantity=abs(v), signed_quantity=v, fill_price=v,
                notional=v, fee=Decimal("0"),
                position_qty_after=v,
            ))
        await session.commit()

    async with sf() as session:
        rows = (await session.execute(select(FillLog).order_by(FillLog.id))).scalars().all()
    got = [r.fill_price for r in rows]
    assert all(isinstance(x, Decimal) for x in got)
    assert got == _EXACT_CASES, f"Decimal round-trip not exact on SQLite: {got}"


@pytest.mark.asyncio
async def test_position_quantity_sum_is_exact_on_sqlite(sf):
    # Three 0.1 buys + one 1e-8 buy. Python-exact total = 0.30000001.
    # A double-precision SQL SUM would drift (e.g. 0.30000000999...).
    for _ in range(3):
        await record_fill(sf, broker="ibkr", symbol="ETH", side="buy",
                          quantity=Decimal("0.10000000"), fill_price=Decimal("3000"))
    await record_fill(sf, broker="ibkr", symbol="ETH", side="buy",
                      quantity=Decimal("0.00000001"), fill_price=Decimal("3000"))
    qty = await available_quantity(sf, "ibkr", "ETH")
    assert qty == Decimal("0.30000001"), f"position SUM not exact: {qty!r}"


@pytest.mark.asyncio
async def test_open_position_abs_filter_on_sqlite(sf):
    async with sf() as session:
        for i, q in enumerate([Decimal("0"), Decimal("0.00000001"), Decimal("-5.25")]):
            session.add(PositionLog(
                id=i + 1, timestamp=datetime.now(timezone.utc),
                symbol="X", broker="ibkr", quantity=q,
                avg_entry_price=Decimal("1"), current_price=Decimal("1"),
                unrealised_pnl=Decimal("0"), asset_class="equity",
            ))
        await session.commit()

    eps = Decimal("0.00000001")
    async with sf() as session:
        gt = (await session.execute(
            select(PositionLog.id).where(func.abs(PositionLog.quantity) > eps)
        )).scalars().all()
        ge = (await session.execute(
            select(PositionLog.id).where(func.abs(PositionLog.quantity) >= eps)
        )).scalars().all()
    assert gt == [3]          # only the -5.25 short exceeds eps
    assert sorted(ge) == [2, 3]
