"""Repair helpers for the local paper ``PositionLog`` book."""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import func, select


@dataclass(frozen=True)
class LocalPaperFlattenPreview:
    broker: str
    symbol: str
    side: str
    quantity: Decimal
    price: Decimal
    notional: Decimal


@dataclass(frozen=True)
class LocalPaperFlattenResult:
    previews: list[LocalPaperFlattenPreview]
    applied: bool

    @property
    def count(self) -> int:
        return len(self.previews)


def normalize_broker_filter(raw: str | set[str] | None) -> set[str]:
    if raw is None:
        return set()
    if isinstance(raw, set):
        return {str(x).strip().lower() for x in raw if str(x).strip()}
    return {x.strip().lower() for x in str(raw).split(",") if x.strip()}


def refuse_live_local_paper_flatten() -> None:
    env = (os.getenv("APP_ENV", "paper") or "paper").strip().lower()
    if env == "live":
        raise RuntimeError("APP_ENV=live - refusing to flatten local paper ledger")


async def latest_open_local_paper_rows(session_factory, *, brokers: set[str] | None = None) -> list:
    """Return latest non-zero ``PositionLog`` rows per broker/symbol."""
    from storage.models import PositionLog

    broker_filter = normalize_broker_filter(brokers)
    async with session_factory() as session:
        latest_by_key = (
            select(
                PositionLog.broker.label("broker"),
                PositionLog.symbol.label("symbol"),
                func.max(PositionLog.timestamp).label("max_ts"),
            )
            .group_by(PositionLog.broker, PositionLog.symbol)
            .subquery()
        )
        stmt = (
            select(PositionLog)
            .join(
                latest_by_key,
                (PositionLog.broker == latest_by_key.c.broker)
                & (PositionLog.symbol == latest_by_key.c.symbol)
                & (PositionLog.timestamp == latest_by_key.c.max_ts),
            )
            .where(func.abs(PositionLog.quantity) > Decimal("0.00000001"))
            .order_by(PositionLog.broker.asc(), PositionLog.symbol.asc())
        )
        if broker_filter:
            stmt = stmt.where(func.lower(PositionLog.broker).in_(broker_filter))
        return list((await session.execute(stmt)).scalars().all())


def normalize_symbol_filter(raw: str | set[str] | None) -> set[str]:
    if raw is None:
        return set()
    if isinstance(raw, set):
        return {str(x).strip().upper() for x in raw if str(x).strip()}
    return {x.strip().upper() for x in str(raw).split(",") if x.strip()}


def _preview(row) -> LocalPaperFlattenPreview:
    qty = Decimal(str(row.quantity or "0"))
    px = Decimal(str(row.current_price or row.avg_entry_price or "0"))
    side = "sell" if qty > 0 else "buy"
    return LocalPaperFlattenPreview(
        broker=str(row.broker or "").strip().lower(),
        symbol=str(row.symbol or "").strip().upper(),
        side=side,
        quantity=abs(qty),
        price=px,
        notional=abs(qty) * px,
    )


async def flatten_local_paper_book(
    *,
    apply: bool,
    brokers: set[str] | str | None = None,
    symbols: set[str] | str | None = None,
    reason: str = "local_paper_book_repair",
) -> LocalPaperFlattenResult:
    """Flatten the append-only local paper book with close rows and tombstones.

    This intentionally does not talk to broker adapters. It is only for paper
    mode repair paths where ``PositionLog`` is the simulated book of record.
    """
    refuse_live_local_paper_flatten()

    from storage.db import dispose_engine, init_async_database
    from storage.fills_ledger import record_fill
    from storage.models import OrderLog, PositionLog

    engine, session_factory = await init_async_database()
    if session_factory is None:
        raise RuntimeError("database unavailable")
    try:
        rows = await latest_open_local_paper_rows(
            session_factory,
            brokers=normalize_broker_filter(brokers),
        )
        symbol_filter = normalize_symbol_filter(symbols)
        if symbol_filter:
            rows = [
                row for row in rows
                if str(getattr(row, "symbol", "") or "").strip().upper() in symbol_filter
            ]
        previews = [_preview(row) for row in rows]
        if not apply or not rows:
            return LocalPaperFlattenResult(previews=previews, applied=False)

        now = datetime.now(timezone.utc)
        fill_specs = []
        async with session_factory() as session:
            for row, item in zip(rows, previews, strict=True):
                signal_id = str(uuid.uuid4())
                order_id = str(uuid.uuid4())
                broker_order_id = f"paper-local-flatten-{order_id[:12]}"
                metadata = {
                    "reduce_only": True,
                    "close_only": True,
                    "flatten_all": True,
                    "flatten_reason": reason,
                    "source_position_id": row.id,
                }
                session.add(
                    OrderLog(
                        id=order_id,
                        broker_order_id=broker_order_id,
                        signal_id=signal_id,
                        timestamp=now,
                        symbol=row.symbol,
                        side=item.side,
                        order_type="market",
                        quantity=item.quantity,
                        limit_price=None,
                        broker=row.broker,
                        status="filled",
                        filled_quantity=item.quantity,
                        avg_fill_price=item.price,
                        fee=Decimal("0"),
                        paper_mode=True,
                        instrument_metadata=metadata,
                    )
                )
                session.add(
                    PositionLog(
                        timestamp=now,
                        symbol=row.symbol,
                        broker=row.broker,
                        quantity=Decimal("0"),
                        avg_entry_price=Decimal(str(row.avg_entry_price or item.price)),
                        current_price=item.price,
                        unrealised_pnl=Decimal("0"),
                        asset_class=row.asset_class,
                        instrument_metadata=(
                            row.instrument_metadata
                            if isinstance(row.instrument_metadata, dict)
                            else None
                        ),
                    )
                )
                fill_specs.append(
                    {
                        "broker": row.broker,
                        "symbol": row.symbol,
                        "side": item.side,
                        "quantity": item.quantity,
                        "fill_price": item.price,
                        "asset_class": row.asset_class,
                        "order_type": "market",
                        "reduce_only": True,
                        "strategy": reason,
                        "signal_id": signal_id,
                        "mode": "paper",
                        "is_paper": True,
                        "derisk_source": reason,
                        "order_id": order_id,
                        "broker_order_id": broker_order_id,
                        "instrument_metadata": metadata,
                        "timestamp": now,
                    }
                )
            await session.commit()
        for spec in fill_specs:
            await record_fill(session_factory, **spec)
        return LocalPaperFlattenResult(previews=previews, applied=True)
    finally:
        if engine is not None:
            await dispose_engine(engine)
