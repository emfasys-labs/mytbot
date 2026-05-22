"""
storage/fills_ledger.py
========================
D126 — the clean fills ledger.

This module is the single write-path for the ``fills`` table and the
race-free source of truth for position quantity.

Design
------
* ``record_fill`` is the ONLY way a row enters ``fills``. It is wrapped
  in a process-wide ``asyncio.Lock`` so the read-prior-state /
  compute-WAC / append sequence is atomic — the snapshot-resurrection
  race that caused the pre-D126 phantom oversells (a position sold,
  then a later-timestamped snapshot writer resurrecting the pre-sell
  quantity) cannot occur on an append-only ledger guarded by one lock.

* A position's quantity for ``(broker, symbol)`` is exactly
  ``SUM(signed_quantity)`` over its fills — see ``available_quantity``.
  This is deterministic and never races.

* Realised P&L uses **weighted-average cost** (WAC). ``realised_pnl`` is
  GROSS trading P&L (fee is a separate column); net P&L for any slice
  is ``SUM(realised_pnl) - SUM(fee)``.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

from sqlalchemy import func, select

from storage.models import FillLog

logger = logging.getLogger(__name__)

# One global lock. Fill volume is modest (~150/day) so per-symbol
# striping buys nothing; a single lock keeps the critical section
# trivially correct.
_FILLS_LOCK = asyncio.Lock()

_ZERO = Decimal("0")


def _dec(value: Any, default: Decimal = _ZERO) -> Decimal:
    if value is None:
        return default
    try:
        d = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return default
    if d != d or d in (Decimal("Infinity"), Decimal("-Infinity")):
        return default
    return d


async def available_quantity(session_factory, broker: str, symbol: str) -> Decimal:
    """Race-free current signed position quantity = SUM(signed_quantity).

    Positive = net long, negative = net short, zero = flat. This is the
    authoritative holding figure; ``positions`` snapshots are display
    only and may lag.
    """
    if session_factory is None:
        return _ZERO
    b = (broker or "").strip().lower()
    s = (symbol or "").strip().upper()
    if not b or not s:
        return _ZERO
    async with session_factory() as session:
        q = await session.execute(
            select(func.coalesce(func.sum(FillLog.signed_quantity), 0)).where(
                FillLog.broker == b, FillLog.symbol == s
            )
        )
        return _dec(q.scalar())


async def position_state(session_factory, broker: str, symbol: str) -> tuple[Decimal, int]:
    """Return ``(signed_qty, fill_count)`` for ``(broker, symbol)``.

    ``fill_count == 0`` means the ledger has no opinion on this symbol
    yet — callers (e.g. the oversell guard) should treat the ledger as
    non-authoritative and fall back to legacy behaviour rather than
    blocking a close. Post data-reset the ledger records every fill
    from T0, so ``fill_count`` is always > 0 for any held symbol.
    """
    if session_factory is None:
        return (_ZERO, 0)
    b = (broker or "").strip().lower()
    s = (symbol or "").strip().upper()
    if not b or not s:
        return (_ZERO, 0)
    async with session_factory() as session:
        q = await session.execute(
            select(
                func.coalesce(func.sum(FillLog.signed_quantity), 0),
                func.count(FillLog.id),
            ).where(FillLog.broker == b, FillLog.symbol == s)
        )
        row = q.first()
        if row is None:
            return (_ZERO, 0)
        return (_dec(row[0]), int(row[1] or 0))


async def _prior_state(session, broker: str, symbol: str) -> tuple[Decimal, Decimal, Optional[datetime]]:
    """Return (prior_signed_qty, prior_avg_cost, position_opened_at).

    ``position_opened_at`` is the timestamp of the fill that started the
    current open run (the first fill after the last time the position
    was flat). ``None`` when the position is currently flat.
    """
    last_q = await session.execute(
        select(FillLog)
        .where(FillLog.broker == broker, FillLog.symbol == symbol)
        .order_by(FillLog.id.desc())
        .limit(1)
    )
    last = last_q.scalars().first()
    if last is None:
        return (_ZERO, _ZERO, None)
    prior_qty = _dec(last.position_qty_after)
    prior_avg = _dec(last.avg_cost_basis)
    if prior_qty == 0:
        return (_ZERO, _ZERO, None)
    # Opening fill = earliest fill with id greater than the last id at
    # which the position was flat (position_qty_after == 0).
    last_flat_id_q = await session.execute(
        select(func.coalesce(func.max(FillLog.id), 0)).where(
            FillLog.broker == broker,
            FillLog.symbol == symbol,
            FillLog.position_qty_after == 0,
        )
    )
    last_flat_id = int(last_flat_id_q.scalar() or 0)
    open_q = await session.execute(
        select(FillLog.timestamp)
        .where(
            FillLog.broker == broker,
            FillLog.symbol == symbol,
            FillLog.id > last_flat_id,
        )
        .order_by(FillLog.id.asc())
        .limit(1)
    )
    opened_at = open_q.scalar()
    return (prior_qty, prior_avg, opened_at)


def _slippage_bps(
    intended: Optional[Decimal],
    fill_price: Decimal,
    signed_delta: Decimal,
) -> Optional[Decimal]:
    """Signed adverse slippage in basis points, or ``None`` when unknown.

    Convention: **positive = the fill was WORSE than intended** (an
    execution cost), **negative = price improvement**. For a buy, paying
    more than intended is adverse; for a sell, receiving less is adverse.
    Returns ``None`` when no usable intended price was supplied.
    """
    if intended is None or intended <= 0 or fill_price <= 0:
        return None
    if signed_delta > 0:        # buy — adverse when fill > intended
        raw = (fill_price - intended) / intended
    else:                       # sell — adverse when fill < intended
        raw = (intended - fill_price) / intended
    return (raw * Decimal("10000")).quantize(Decimal("0.0001"))


def _compute_wac(
    prior_qty: Decimal,
    prior_avg: Decimal,
    signed_delta: Decimal,
    fill_price: Decimal,
) -> tuple[Decimal, Decimal, Decimal, bool]:
    """Weighted-average-cost P&L step.

    Returns ``(new_qty, new_avg_cost, realised_pnl, is_closing)``.
    ``realised_pnl`` is GROSS (no fee). ``is_closing`` is True when this
    fill reduced |position| (so a holding period applies).
    """
    new_qty = prior_qty + signed_delta

    if prior_qty == 0:
        # Opening a fresh position.
        return (new_qty, fill_price, _ZERO, False)

    same_direction = (prior_qty > 0 and signed_delta > 0) or (prior_qty < 0 and signed_delta < 0)
    if same_direction:
        # Adding to an existing position — weighted-average the cost.
        denom = abs(new_qty)
        if denom == 0:
            return (new_qty, prior_avg, _ZERO, False)
        new_avg = (abs(prior_qty) * prior_avg + abs(signed_delta) * fill_price) / denom
        return (new_qty, new_avg, _ZERO, False)

    # Opposite direction — reducing, exactly closing, or flipping.
    closed_qty = min(abs(signed_delta), abs(prior_qty))
    if prior_qty > 0:
        # Long being sold.
        realised = (fill_price - prior_avg) * closed_qty
    else:
        # Short being covered.
        realised = (prior_avg - fill_price) * closed_qty

    if abs(signed_delta) <= abs(prior_qty):
        # Pure reduce (or exact close). Remaining lot keeps its cost.
        new_avg = prior_avg if new_qty != 0 else _ZERO
    else:
        # Flip: position crossed zero; the excess opens a new position
        # in the opposite direction at this fill's price.
        new_avg = fill_price
    return (new_qty, new_avg, realised, True)


async def record_fill(
    session_factory,
    *,
    broker: str,
    symbol: str,
    side: str,
    quantity: Any,
    fill_price: Any,
    intended_price: Any = None,
    fee: Any = 0,
    asset_class: str = "",
    order_type: str = "",
    reduce_only: bool = False,
    strategy: Optional[str] = None,
    signal_id: Optional[str] = None,
    signal_confidence: Any = None,
    mode: Optional[str] = None,
    is_paper: bool = True,
    run_session_id: Optional[str] = None,
    derisk_source: Optional[str] = None,
    order_id: Optional[str] = None,
    broker_order_id: Optional[str] = None,
    instrument_metadata: Optional[dict] = None,
    timestamp: Optional[datetime] = None,
) -> Optional[FillLog]:
    """Append one confirmed fill to the ledger and return the row.

    Atomic under ``_FILLS_LOCK``: prior-state read, WAC computation, and
    the append happen in one critical section, so concurrent monitor /
    loop coroutines can never interleave to corrupt the ledger.

    Returns ``None`` on bad input (non-positive quantity/price) or when
    the DB is unavailable.
    """
    if session_factory is None:
        return None
    b = (broker or "").strip().lower()
    s = (symbol or "").strip().upper()
    qty_abs = abs(_dec(quantity))
    px = _dec(fill_price)
    if not b or not s or qty_abs <= 0 or px <= 0:
        logger.warning(
            "fills_ledger | rejected bad fill | broker=%s symbol=%s qty=%s price=%s",
            b, s, quantity, fill_price,
        )
        return None

    side_l = (side or "").strip().lower()
    signed_delta = qty_abs if side_l in ("buy", "long") else -qty_abs
    ts = timestamp or datetime.now(timezone.utc)
    fee_d = abs(_dec(fee))
    # D130 — execution-quality capture. ``intended_price`` is the signal's
    # target price at order time; slippage is computed against the actual
    # fill. Both stay NULL when no usable intended price was supplied.
    intended_d = _dec(intended_price, default=None) if intended_price is not None else None
    if intended_d is not None and intended_d <= 0:
        intended_d = None
    slippage = _slippage_bps(intended_d, px, signed_delta)

    async with _FILLS_LOCK:
        async with session_factory() as session:
            prior_qty, prior_avg, opened_at = await _prior_state(session, b, s)
            new_qty, new_avg, realised, is_closing = _compute_wac(
                prior_qty, prior_avg, signed_delta, px
            )
            holding_sec: Optional[Decimal] = None
            if is_closing and opened_at is not None:
                opened = opened_at if opened_at.tzinfo else opened_at.replace(tzinfo=timezone.utc)
                holding_sec = Decimal(str(round((ts - opened).total_seconds(), 2)))

            conf_d = _dec(signal_confidence, default=None) if signal_confidence is not None else None

            row = FillLog(
                timestamp=ts,
                broker=b[:20],
                symbol=s[:72],
                asset_class=(asset_class or "").strip().lower()[:20],
                side="buy" if signed_delta > 0 else "sell",
                order_type=(order_type or "").strip().lower()[:20],
                quantity=qty_abs,
                signed_quantity=signed_delta,
                fill_price=px,
                notional=qty_abs * px,
                intended_price=intended_d,
                slippage_bps=slippage,
                fee=fee_d,
                reduce_only=bool(reduce_only),
                realised_pnl=realised,
                avg_cost_basis=new_avg,
                position_qty_after=new_qty,
                holding_period_sec=holding_sec,
                strategy=(strategy or None),
                signal_id=(signal_id or None),
                signal_confidence=conf_d,
                mode=(mode or None),
                is_paper=bool(is_paper),
                run_session_id=(run_session_id or None),
                derisk_source=(derisk_source or None),
                order_id=(order_id or None),
                broker_order_id=(broker_order_id or None),
                instrument_metadata=instrument_metadata if isinstance(instrument_metadata, dict) else None,
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return row
