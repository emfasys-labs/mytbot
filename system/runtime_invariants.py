"""Continuous accounting and execution invariants for the running system."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from storage.models import FillLog, OrderLog, PositionLog, TradeAdmissionLog
from core.instrument_semantics import InstrumentRole, instrument_role
from portfolio.balance import BalancePolicy, economic_key, legacy_reconciliation_plan
from portfolio.cluster_map import economic_factor_loadings


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
            PositionLog.avg_entry_price.label("avg_entry_price"),
            PositionLog.current_price.label("current_price"),
            PositionLog.asset_class.label("asset_class"),
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
                    ranked_positions.c.avg_entry_price,
                    ranked_positions.c.current_price,
                    ranked_positions.c.asset_class,
                ).where(ranked_positions.c.rn == 1)
            )
        ).all()
        position_qty = {
            (str(row.broker), str(row.symbol)): _dec(row.quantity)
            for row in position_rows
        }

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

        active_rows: list[dict[str, Any]] = []
        for row in position_rows:
            quantity = _dec(row.quantity)
            price = _dec(row.current_price or row.avg_entry_price)
            if quantity == 0 or price <= 0:
                continue
            active_rows.append(
                {
                    "broker": str(row.broker or "").strip().lower(),
                    "symbol": str(row.symbol or "").strip().upper(),
                    "quantity": quantity,
                    "avg_entry_price": _dec(row.avg_entry_price),
                    "current_price": price,
                    "asset_class": str(row.asset_class or "").strip().lower(),
                    "notional": abs(quantity) * price,
                }
            )

        economic_groups: dict[str, list[dict[str, Any]]] = {}
        for row in active_rows:
            economic_groups.setdefault(economic_key(row["symbol"]), []).append(row)
        duplicate_economic_positions = [
            {
                "economic_symbol": symbol,
                "brokers": sorted({row["broker"] for row in rows}),
                "rows": len(rows),
                "gross_notional": str(
                    sum((row["notional"] for row in rows), Decimal("0"))
                ),
            }
            for symbol, rows in sorted(economic_groups.items())
            if len(rows) > 1
        ]

        cash_equivalent_alpha_positions = [
            {
                "broker": row["broker"],
                "symbol": row["symbol"],
                "notional": str(row["notional"]),
            }
            for row in active_rows
            if instrument_role(
                row["symbol"], asset_class=row["asset_class"]
            )
            == InstrumentRole.CASH_EQUIVALENT
        ]
        liquidity_reserve_positions = [
            {
                "broker": row["broker"],
                "symbol": row["symbol"],
                "notional": str(row["notional"]),
            }
            for row in active_rows
            if instrument_role(
                row["symbol"], asset_class=row["asset_class"]
            )
            == InstrumentRole.LIQUIDITY_RESERVE
        ]

        factor_members: dict[str, set[str]] = {}
        factor_notional: dict[str, Decimal] = {}
        for row in active_rows:
            for factor, loading in economic_factor_loadings(
                row["symbol"], row["asset_class"]
            ).items():
                if factor.startswith("instrument:"):
                    continue
                factor_members.setdefault(factor, set()).add(
                    economic_key(row["symbol"])
                )
                factor_notional[factor] = factor_notional.get(
                    factor, Decimal("0")
                ) + row["notional"] * abs(loading)
        overlapping_factor_groups = [
            {
                "factor": factor,
                "symbols": sorted(symbols),
                "notional": str(factor_notional.get(factor, Decimal("0"))),
            }
            for factor, symbols in sorted(factor_members.items())
            if len(symbols) > 1 and factor != "usd_factor"
        ]

        gross_notional = sum(
            (row["notional"] for row in active_rows), Decimal("0")
        )
        broker_notional: dict[str, Decimal] = {}
        for row in active_rows:
            broker_notional[row["broker"]] = broker_notional.get(
                row["broker"], Decimal("0")
            ) + row["notional"]
        policy = BalancePolicy()
        broker_concentration = [
            {
                "broker": broker,
                "notional": str(notional),
                "gross_share": str(notional / gross_notional),
            }
            for broker, notional in sorted(broker_notional.items())
            if gross_notional > 0
            and notional / gross_notional > policy.broker_concentration_warn
        ]
        tiny_positions = [
            {
                "broker": row["broker"],
                "symbol": row["symbol"],
                "notional": str(row["notional"]),
            }
            for row in active_rows
            if gross_notional > 0
            and row["notional"] / gross_notional < policy.tiny_position_nav_pct
        ]
        reconciliation_plan = legacy_reconciliation_plan(
            active_rows,
            nav=gross_notional,
            policy=policy,
        )

    healthy = not (
        mismatches
        or filled_without_fill
        or stale_working_orders
        or recent_unpriced_outcomes
        or duplicate_economic_positions
        or cash_equivalent_alpha_positions
        or overlapping_factor_groups
        or broker_concentration
        or tiny_positions
    )
    return {
        "healthy": healthy,
        "checked_at": now.isoformat(),
        "fill_position_mismatches": mismatches[:20],
        "filled_orders_without_fills": filled_without_fill,
        "stale_working_orders": stale_working_orders,
        "recent_unpriced_outcomes": recent_unpriced_outcomes,
        "duplicate_economic_positions": duplicate_economic_positions[:20],
        "cash_equivalent_alpha_positions": cash_equivalent_alpha_positions[:20],
        "liquidity_reserve_positions": liquidity_reserve_positions[:20],
        "overlapping_factor_groups": overlapping_factor_groups[:20],
        "broker_concentration": broker_concentration,
        "tiny_positions": tiny_positions[:20],
        "legacy_reconciliation_plan": reconciliation_plan[:20],
    }
