"""
Upsert helpers for M2 tables (PostgreSQL ON CONFLICT).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Sequence

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from storage.models import FeatureSnapshot, MacroObservation, NewsHeadline


def _dec(x: Any) -> Decimal:
    return x if isinstance(x, Decimal) else Decimal(str(x))


async def upsert_feature_snapshots(
    session: AsyncSession,
    rows: Sequence[dict[str, Any]],
) -> int:
    if not rows:
        return 0
    cleaned = []
    for r in rows:
        cleaned.append(
            {
                "symbol": r["symbol"],
                "timeframe": r["timeframe"],
                "bar_timestamp": r["bar_timestamp"],
                "open": _dec(r["open"]),
                "high": _dec(r["high"]),
                "low": _dec(r["low"]),
                "close": _dec(r["close"]),
                "volume": _dec(r["volume"]),
                "features": r["features"],
                "validation": r.get("validation"),
                "data_source": r.get("data_source", "yfinance"),
            }
        )
    stmt = pg_insert(FeatureSnapshot).values(cleaned)
    stmt = stmt.on_conflict_do_update(
        constraint="uq_feature_snapshots_symbol_tf_bar_ts",
        set_={
            "open": stmt.excluded.open,
            "high": stmt.excluded.high,
            "low": stmt.excluded.low,
            "close": stmt.excluded.close,
            "volume": stmt.excluded.volume,
            "features": stmt.excluded.features,
            "validation": stmt.excluded.validation,
            "data_source": stmt.excluded.data_source,
        },
    )
    await session.execute(stmt)
    return len(cleaned)


async def insert_news_ignore_duplicates(
    session: AsyncSession,
    rows: Sequence[dict[str, Any]],
) -> int:
    """Returns number of rows attempted (not how many were new)."""
    if not rows:
        return 0
    for r in rows:
        stmt = pg_insert(NewsHeadline).values(r)
        stmt = stmt.on_conflict_do_nothing(index_elements=["content_hash"])
        await session.execute(stmt)
    return len(rows)


async def upsert_macro_observations(
    session: AsyncSession,
    rows: Sequence[dict[str, Any]],
) -> int:
    if not rows:
        return 0
    cleaned = []
    for r in rows:
        cleaned.append(
            {
                "series_id": r["series_id"],
                "obs_date": r["obs_date"],
                "value": _dec(r["value"]),
                "fetched_at": r["fetched_at"],
            }
        )
    stmt = pg_insert(MacroObservation).values(cleaned)
    stmt = stmt.on_conflict_do_update(
        constraint="uq_macro_series_date",
        set_={
            "value": stmt.excluded.value,
            "fetched_at": stmt.excluded.fetched_at,
        },
    )
    await session.execute(stmt)
    return len(cleaned)


async def count_feature_bars(
    session: AsyncSession,
    *,
    symbol: str,
    timeframe: str,
) -> int:
    q = await session.execute(
        select(func.count())
        .select_from(FeatureSnapshot)
        .where(
            FeatureSnapshot.symbol == symbol,
            FeatureSnapshot.timeframe == timeframe,
        )
    )
    return int(q.scalar_one())
