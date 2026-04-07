from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from storage.models import AnomalyLog, ThesisLog


def _to_dt(ts: str | None) -> datetime:
    if not ts:
        return datetime.now(timezone.utc)
    t = ts
    if t.endswith("Z"):
        t = t[:-1] + "+00:00"
    dt = datetime.fromisoformat(t)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


async def persist_anomaly_log(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    anomaly,
    opportunities_found: int | None = None,
    thesis_generated: bool = False,
    signals_produced: int | None = None,
) -> None:
    row = AnomalyLog(
        timestamp=_to_dt(getattr(anomaly, "timestamp", None)),
        symbol=str(anomaly.symbol)[:20],
        asset_class=str(anomaly.asset_class)[:20],
        direction=str(anomaly.direction)[:8],
        price_move_pct=Decimal(str(anomaly.price_move_pct)),
        price_z_score=Decimal(str(anomaly.price_z_score)),
        volume_z_score=Decimal(str(anomaly.volume_z_score)),
        news_velocity=Decimal(str(anomaly.news_velocity)),
        news_sentiment=Decimal(str(anomaly.news_sentiment)),
        anomaly_score=Decimal(str(anomaly.anomaly_score)),
        opportunities_found=opportunities_found,
        thesis_generated=thesis_generated,
        signals_produced=signals_produced,
    )
    async with session_factory() as session:
        session.add(row)
        await session.commit()


async def persist_thesis_log(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    thesis,
    ai_cost_usd: Decimal | None = None,
) -> None:
    row = ThesisLog(
        timestamp=_to_dt(getattr(thesis, "generated_at", None)),
        trigger_symbol=str(thesis.trigger_symbol)[:20],
        trigger_direction=str(thesis.trigger_direction)[:8],
        trigger_explanation=str(thesis.trigger_explanation),
        overall_confidence=Decimal(str(thesis.overall_confidence)),
        time_horizon_hours=int(thesis.time_horizon_hours),
        opportunities=getattr(thesis, "priority_opportunities", None),
        invalidation_conditions=getattr(thesis, "invalidation_conditions", None),
        model_used=str(thesis.model_used)[:64],
        tokens_used=int(thesis.tokens_used),
        ai_cost_usd=ai_cost_usd,
    )
    async with session_factory() as session:
        session.add(row)
        await session.commit()
