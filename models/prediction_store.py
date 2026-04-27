"""
models/prediction_store.py
============================
Wave 1 — async writer/reader for the ``model_predictions`` table.

Design:

- The store does not own a session factory. Callers pass a
  ``async_sessionmaker`` (the same one bound by the FastAPI lifespan
  via ``storage.db.bind_app_database``) so we share a single connection
  pool with the rest of the app.
- ``write_prediction`` enforces the as-of safety invariant. In ``live``
  mode a leakage detection raises; in ``paper`` it raises too because
  bad data must not pollute soak metrics; in ``research`` it raises as
  well — the invariant is non-negotiable. The mode parameter only
  affects what callers do *before* writing.
- The Decimal columns store probability/return/volatility/confidence
  exactly. We do not silently coerce floats — callers must pass Decimal
  (or None).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from models.feature_contracts import require_as_of_safe
from models.schemas import Mode, Prediction
from storage.models import ModelPrediction

logger = logging.getLogger(__name__)


def _coerce_decimal(value: Optional[Decimal | str]) -> Optional[Decimal]:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    # Accept str only — refusing float keeps the no-float invariant
    # documented in CLAUDE.md ("Decimal for all prices and quantities,
    # never float"). Callers that want to write a learned-from-floats
    # number must convert deliberately.
    if isinstance(value, str):
        return Decimal(value)
    raise TypeError(
        f"Decimal-typed prediction field must be Decimal | str | None, "
        f"got {type(value).__name__}"
    )


async def write_prediction(
    session_factory: async_sessionmaker[AsyncSession],
    prediction: Prediction,
    *,
    as_of_tolerance_seconds: float = 0.0,
) -> int:
    """
    Persist a prediction. Returns the new row id.

    Raises ``AsOfLeakageError`` if ``as_of_ts > prediction_ts``.
    """
    require_as_of_safe(
        as_of_ts=prediction.as_of_ts,
        prediction_ts=prediction.prediction_ts,
        tolerance_seconds=as_of_tolerance_seconds,
    )

    row = ModelPrediction(
        model_name=prediction.model_name,
        model_version=prediction.model_version,
        symbol=prediction.symbol,
        as_of_ts=prediction.as_of_ts,
        prediction_ts=prediction.prediction_ts,
        horizon_seconds=prediction.horizon_seconds,
        horizon_bars=prediction.horizon_bars,
        predicted_probability=_coerce_decimal(prediction.predicted_probability),
        expected_return=_coerce_decimal(prediction.expected_return),
        expected_volatility=_coerce_decimal(prediction.expected_volatility),
        confidence=_coerce_decimal(prediction.confidence),
        feature_hash=prediction.feature_hash,
        mode=prediction.mode.value if isinstance(prediction.mode, Mode) else str(prediction.mode),
        metadata_=dict(prediction.metadata) if prediction.metadata else None,
    )

    async with session_factory() as session:
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return int(row.id)


async def read_predictions(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    model_name: Optional[str] = None,
    model_version: Optional[str] = None,
    symbol: Optional[str] = None,
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """
    Read predictions matching the filter. Returned as plain dicts so the
    caller does not need to import ORM rows.
    """
    stmt = select(ModelPrediction)
    if model_name:
        stmt = stmt.where(ModelPrediction.model_name == model_name)
    if model_version:
        stmt = stmt.where(ModelPrediction.model_version == model_version)
    if symbol:
        stmt = stmt.where(ModelPrediction.symbol == symbol)
    if since:
        stmt = stmt.where(ModelPrediction.prediction_ts >= since)
    if until:
        stmt = stmt.where(ModelPrediction.prediction_ts <= until)
    stmt = stmt.order_by(ModelPrediction.prediction_ts.desc()).limit(limit)

    async with session_factory() as session:
        rows = (await session.execute(stmt)).scalars().all()

    out: list[dict[str, Any]] = []
    for r in rows:
        out.append(
            {
                "id": int(r.id),
                "model_name": r.model_name,
                "model_version": r.model_version,
                "symbol": r.symbol,
                "as_of_ts": _to_utc(r.as_of_ts),
                "prediction_ts": _to_utc(r.prediction_ts),
                "horizon_seconds": r.horizon_seconds,
                "horizon_bars": r.horizon_bars,
                "predicted_probability": r.predicted_probability,
                "expected_return": r.expected_return,
                "expected_volatility": r.expected_volatility,
                "confidence": r.confidence,
                "feature_hash": r.feature_hash,
                "mode": r.mode,
                "metadata": r.metadata_ or {},
            }
        )
    return out


def _to_utc(ts: Optional[datetime]) -> Optional[datetime]:
    if ts is None:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
