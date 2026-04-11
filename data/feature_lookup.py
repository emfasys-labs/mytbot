"""Load latest feature JSON from M2 feature_snapshots for D015."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from storage.models import FeatureSnapshot


async def load_latest_feature_json(
    session: AsyncSession,
    symbol: str,
    timeframe: str,
) -> dict[str, Any] | None:
    """
    Return ``features`` JSON for the latest bar of (symbol, timeframe), or None.
    """
    sym = symbol[:32]
    tf = timeframe[:8]
    stmt = (
        select(FeatureSnapshot.features)
        .where(FeatureSnapshot.symbol == sym, FeatureSnapshot.timeframe == tf)
        .order_by(FeatureSnapshot.bar_timestamp.desc())
        .limit(1)
    )
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        return None
    return dict(row) if isinstance(row, dict) else None


async def load_latest_features_for_symbols(
    session: AsyncSession,
    symbols: list[str],
    timeframe: str,
) -> dict[str, dict[str, Any]]:
    """Batch: latest feature dict per symbol (one query per symbol; OK for modest N)."""
    out: dict[str, dict[str, Any]] = {}
    for s in symbols:
        fj = await load_latest_feature_json(session, s, timeframe)
        if fj is not None:
            out[s] = fj
            out.setdefault(s.upper(), fj)
    return out
