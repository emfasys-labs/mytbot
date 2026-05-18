"""Calendar week / month rollups over ``DailyPnL`` (ISO date strings)."""

from __future__ import annotations

import os
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import and_, func, select

from storage.models import DailyPnL

# Production-P&L start. Rows before this are the pre-instrumentation
# bring-up period: their realised/unrealised/fees were deliberately
# zeroed (operator-approved rectification), but their fill *counts* are
# truthful. Including those rows in period rollups makes "month/all-time
# trades" span a wider window than "month/all-time P&L/fees" — the
# semantic split Codex flagged. Excluding them from ALL rollups makes
# every period metric measure the same production window.
PRODUCTION_PNL_START = os.getenv("PRODUCTION_PNL_START", "2026-05-13")


def week_to_date_range(today: date) -> tuple[date, date]:
    """Monday (inclusive) through ``today`` (inclusive), same calendar week."""
    monday = today - timedelta(days=today.weekday())
    return monday, today


def month_to_date_range(today: date) -> tuple[date, date]:
    start = today.replace(day=1)
    return start, today


def merge_live_today_unrealised_into_period(
    period_unrealised: Decimal,
    *,
    db_today_unrealised: Decimal,
    live_today_unrealised: Decimal,
) -> Decimal:
    """
    Unrealised P&L is a point-in-time mark, not an additive daily flow.

    Keep the helper signature for existing callers/tests, but treat the live
    mark as authoritative when present. ``db_today_unrealised`` is intentionally
    ignored; subtracting/summing daily unrealised values double-counts open P&L.
    """
    return live_today_unrealised if live_today_unrealised != 0 else period_unrealised


async def aggregate_daily_pnl_range(session: Any, start: date, end: date) -> dict[str, Any]:
    # Clamp the window start to the production cutoff so counts AND P&L
    # cover the same period (pre-cutoff non-production rows excluded).
    start_s = max(start.isoformat(), PRODUCTION_PNL_START)
    end_s = end.isoformat()
    if start_s > end_s:  # window entirely pre-production → empty rollup
        return {
            "realised": "0", "unrealised": "0", "fees": "0", "trades": 0,
            "period_start": start_s, "period_end": end_s,
        }
    q = await session.execute(
        select(
            func.coalesce(func.sum(DailyPnL.realised_pnl), 0),
            func.coalesce(func.sum(DailyPnL.total_fees), 0),
            func.coalesce(func.sum(DailyPnL.trade_count), 0),
        ).where(and_(DailyPnL.date >= start_s, DailyPnL.date <= end_s))
    )
    row = q.one()
    latest_u_q = await session.execute(
        select(DailyPnL.unrealised_pnl)
        .where(and_(DailyPnL.date >= start_s, DailyPnL.date <= end_s))
        .order_by(DailyPnL.date.desc())
        .limit(1)
    )
    latest_u = latest_u_q.scalar_one_or_none()
    return {
        "realised": str(Decimal(str(row[0] or 0))),
        "unrealised": str(Decimal(str(latest_u or 0))),
        "fees": str(Decimal(str(row[1] or 0))),
        "trades": int(row[2] or 0),
        "period_start": start_s,
        "period_end": end_s,
    }


def equity_max_drawdown_pct(values: list[Decimal]) -> float | None:
    """Peak-to-trough drawdown on a portfolio value series (percent)."""
    if len(values) < 5:
        return None
    peak = values[0]
    max_dd = Decimal(0)
    for v in values:
        if v > peak:
            peak = v
        if peak > 0:
            dd = (peak - v) / peak
            if dd > max_dd:
                max_dd = dd
    return float(max_dd * Decimal("100"))


async def win_rate_from_daily_rows(session: Any, *, limit_days: int = 120) -> float | None:
    """Among days with at least one trade, fraction with positive realised P&L."""
    q = await session.execute(
        select(DailyPnL.realised_pnl, DailyPnL.trade_count)
        .where(DailyPnL.trade_count > 0, DailyPnL.date >= PRODUCTION_PNL_START)
        .order_by(DailyPnL.date.desc())
        .limit(limit_days)
    )
    rows = list(q.all())
    if len(rows) < 5:
        return None
    wins = 0
    total = 0
    for rp, tc in rows:
        total += 1
        try:
            r = Decimal(str(rp or 0))
        except Exception:  # noqa: BLE001
            continue
        if r > 0:
            wins += 1
    if total == 0:
        return None
    return wins / total
