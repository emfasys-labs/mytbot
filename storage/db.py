"""
storage/db.py
=============
Async SQLAlchemy engine, schema creation, TimescaleDB hypertable, tick persistence.
Used by main.py (M1) and integration tests.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from urllib.parse import quote_plus

from loguru import logger
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from brokers.base import Order, OrderResult, Tick
from storage.models import Base, OrderLog, PriceHistory

# FastAPI lifespan binds the primary engine/factory; the trading loop reuses them
# instead of opening a second connection pool to the same database.
_bound_engine: AsyncEngine | None = None
_bound_session_factory: async_sessionmaker[AsyncSession] | None = None


def bind_app_database(
    engine: AsyncEngine | None,
    session_factory: async_sessionmaker[AsyncSession] | None,
) -> None:
    """Register the API-owned engine/session factory for reuse by the trading loop."""
    global _bound_engine, _bound_session_factory
    _bound_engine = engine
    _bound_session_factory = session_factory


def clear_app_database_bind() -> None:
    """Clear bind before disposing the API engine (shutdown)."""
    global _bound_engine, _bound_session_factory
    _bound_engine = None
    _bound_session_factory = None


def get_app_database() -> tuple[AsyncEngine | None, async_sessionmaker[AsyncSession] | None]:
    """Return (engine, session_factory) if API startup registered them, else (None, None)."""
    return _bound_engine, _bound_session_factory


def database_async_url_from_env() -> str | None:
    host = os.getenv("POSTGRES_HOST", "localhost")
    port = os.getenv("POSTGRES_PORT", "5432")
    db = os.getenv("POSTGRES_DB", "mytbot")
    user = os.getenv("POSTGRES_USER", "mytbot")
    password = os.getenv("POSTGRES_PASSWORD", "")
    if not host or not db or not user:
        return None
    u = quote_plus(user)
    p = quote_plus(password)
    return f"postgresql+asyncpg://{u}:{p}@{host}:{port}/{db}"


def tick_timestamp_to_datetime(ts: str) -> datetime:
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    dt = datetime.fromisoformat(ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


async def _try_create_price_hypertable(conn: AsyncConnection) -> None:
    sql_simple = text(
        "SELECT create_hypertable("
        "'price_history', 'timestamp', if_not_exists => TRUE);"
    )
    sql_migrate = text(
        "SELECT create_hypertable("
        "'price_history', 'timestamp', if_not_exists => TRUE, migrate_data => TRUE);"
    )
    try:
        async with conn.begin_nested():
            await conn.execute(sql_simple)
        logger.info("storage | TimescaleDB | price_history hypertable ready")
        return
    except Exception as exc:  # noqa: BLE001
        msg = str(exc).lower()
        if "not empty" in msg and "migrate_data" in msg:
            try:
                async with conn.begin_nested():
                    await conn.execute(sql_migrate)
                logger.info(
                    "storage | TimescaleDB | price_history hypertable ready (migrated existing rows)"
                )
                return
            except Exception as exc2:  # noqa: BLE001
                msg2 = str(exc2).lower()
                if (
                    "cannot create a unique index without the column" in msg2
                    and "timestamp" in msg2
                ):
                    logger.info(
                        "storage | TimescaleDB hypertable skipped | "
                        "existing unique index/primary key on price_history is incompatible "
                        "with partition key timestamp"
                    )
                    return
                logger.warning(
                    "storage | TimescaleDB hypertable migrate retry failed | {}",
                    exc2,
                )
                return
        logger.warning(
            "storage | TimescaleDB hypertable skipped (plain Postgres, not Timescale, or other) | {}",
            exc,
        )


async def init_async_database() -> tuple[AsyncEngine | None, async_sessionmaker[AsyncSession] | None]:
    """
    Create engine, tables, and best-effort Timescale hypertable on price_history.
    Returns (None, None) if POSTGRES_* is incomplete or connection fails.
    """
    url = database_async_url_from_env()
    if not url:
        logger.warning("storage | POSTGRES_* incomplete — persistence disabled")
        return None, None
    engine: AsyncEngine | None = None
    try:
        engine = create_async_engine(url, echo=False)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await _try_create_price_hypertable(conn)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        logger.info(
            "storage | connected | {}@{}:{}/{}",
            os.getenv("POSTGRES_USER", "mytbot"),
            os.getenv("POSTGRES_HOST", "localhost"),
            os.getenv("POSTGRES_PORT", "5432"),
            os.getenv("POSTGRES_DB", "mytbot"),
        )
        return engine, factory
    except Exception as exc:  # noqa: BLE001
        msg = str(exc).lower()
        hint = ""
        if "password authentication failed" in msg:
            hint = (
                " | hint: POSTGRES_PASSWORD in .env must match the DB role. "
                "If .env is correct but login still fails, either run .\\scripts\\apply_postgres_password.ps1 "
                "(sets DB password from .env) or .\\scripts\\reset_postgres_volume.ps1 -Force (wipes volumes), "
                "then restart the API."
            )
        logger.warning("storage | init failed — running without DB | {}{}", exc, hint)
        if engine is not None:
            await engine.dispose()
        return None, None


async def persist_price_tick(
    session_factory: async_sessionmaker[AsyncSession],
    tick: Tick,
    *,
    db_symbol: str,
    broker: str,
) -> None:
    ts = tick_timestamp_to_datetime(tick.timestamp)
    row = PriceHistory(
        timestamp=ts,
        symbol=db_symbol[:20],
        timeframe="tick",
        open=tick.price,
        high=tick.price,
        low=tick.price,
        close=tick.price,
        volume=tick.volume,
        broker=broker[:20],
    )
    async with session_factory() as session:
        session.add(row)
        await session.commit()


async def persist_order_log(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    order: Order,
    result: OrderResult,
    signal_id: str,
    paper_mode: bool,
    broker: str = "ibkr",
) -> None:
    oid = (order.client_order_id or result.client_order_id or result.broker_order_id or "")[:64]
    if not oid:
        oid = f"m1-{uuid.uuid4()}"
    ts = tick_timestamp_to_datetime(result.timestamp)
    im = getattr(order, "instrument_metadata", None)
    row = OrderLog(
        id=oid[:256],
        broker_order_id=(result.broker_order_id or None)[:64] if result.broker_order_id else None,
        signal_id=signal_id[:128],
        timestamp=ts,
        symbol=order.symbol.strip()[:72],
        side=order.side.value[:4],
        order_type=order.order_type.value[:20],
        quantity=order.quantity,
        limit_price=order.limit_price,
        broker=broker[:20],
        status=result.status.value[:20],
        filled_quantity=result.filled_quantity,
        avg_fill_price=result.avg_fill_price,
        fee=result.fee,
        paper_mode=paper_mode,
        instrument_metadata=im if isinstance(im, dict) else None,
    )
    async with session_factory() as session:
        session.add(row)
        await session.commit()


async def dispose_engine(engine: AsyncEngine | None) -> None:
    if engine is not None:
        await engine.dispose()
