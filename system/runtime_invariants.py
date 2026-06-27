"""Continuous accounting and execution invariants for the running system."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from storage.models import FillLog, OrderLog, PositionLog, TradeAdmissionLog


def _dec(value: Any) -> Decimal:
    try:
        return Decimal(str(value or 0))
    except Exception:  # noqa: BLE001
        return Decimal("0")


async def audit_runtime_invariants(
    session_factory: async_sessionmaker[AsyncSession] | None,
    *,
    stale_order_seconds: float,
    outcome_lookback_hours: float = 24.0,
) -> dict[str, Any]:
    if session_factory is None:
        return {"healthy": False, "error": "database_unavailable"}

    now = datetime.now(timezone.utc)
    stale_cutoff = now - timedelta(seconds=max(1.0, float(stale_order_seconds)))
    outcome_cutoff = now - timedelta(hours=max(1.0, float(outcome_lookback_hours)))

    async with session_factory() as session:
        fill_rows = (
            await session.execute(
                select(
                    FillLog.broker,
                    FillLog.symbol,
                    func.sum(FillLog.signed_quantity),
                ).group_by(FillLog.broker, FillLog.symbol)
            )
        ).all()
        fill_qty = {(str(b), str(s)): _dec(q) for b, s, q in fill_rows}

        ranked_positions = select(
            PositionLog.broker.label("broker"),
            PositionLog.symbol.label("symbol"),
            PositionLog.quantity.label("quantity"),
            func.row_number()
            .over(
                partition_by=(PositionLog.broker, PositionLog.symbol),
                order_by=PositionLog.timestamp.desc(),
            )
            .label("rn"),
        ).subquery()
        position_rows = (
            await session.execute(
                select(
                    ranked_positions.c.broker,
                    ranked_positions.c.symbol,
                    ranked_positions.c.quantity,
                ).where(ranked_positions.c.rn == 1)
            )
        ).all()
        position_qty = {(str(b), str(s)): _dec(q) for b, s, q in position_rows}

        mismatches: list[dict[str, str]] = []
        for broker, symbol in sorted(set(fill_qty) | set(position_qty)):
            ledger_qty = fill_qty.get((broker, symbol), Decimal("0"))
            snapshot_qty = position_qty.get((broker, symbol), Decimal("0"))
            if abs(ledger_qty - snapshot_qty) > Decimal("0.000001"):
                mismatches.append(
                    {
                        "broker": broker,
                        "symbol": symbol,
                        "fill_quantity": str(ledger_qty),
                        "position_quantity": str(snapshot_qty),
                    }
                )

        filled_without_fill = int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(OrderLog)
                    .where(
                        OrderLog.status == "filled",
                        ~exists().where(FillLog.order_id == OrderLog.id),
                    )
                )
            ).scalar_one()
            or 0
        )
        stale_working_orders = int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(OrderLog)
                    .where(
                        OrderLog.status.in_(
                            ("submitted", "pending", "partially_filled", "open")
                        ),
                        OrderLog.timestamp < stale_cutoff,
                    )
                )
            ).scalar_one()
            or 0
        )
        recent_unpriced_outcomes = int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(TradeAdmissionLog)
                    .where(
                        TradeAdmissionLog.timestamp >= outcome_cutoff,
                        TradeAdmissionLog.outcome_label == "unpriced",
                    )
                )
            ).scalar_one()
            or 0
        )

    healthy = not (
        mismatches
        or filled_without_fill
        or stale_working_orders
        or recent_unpriced_outcomes
    )
    return {
        "healthy": healthy,
        "checked_at": now.isoformat(),
        "fill_position_mismatches": mismatches[:20],
        "filled_orders_without_fills": filled_without_fill,
        "stale_working_orders": stale_working_orders,
        "recent_unpriced_outcomes": recent_unpriced_outcomes,
    }
