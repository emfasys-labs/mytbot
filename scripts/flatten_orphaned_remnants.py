"""
scripts/flatten_orphaned_remnants.py
=====================================
D115 — One-shot housekeeping tool for the local paper ledger.

After today's churn the book was left with several tiny remnants:
positions that were never properly closed because their notional was
below the system's minimum-order floor for the asset class (e.g. AAPL
qty=46 worth ~$13k after the last partial trim, or QQQ qty=-371 that
no allocator picked up). These remnants accrue carry costs and noise
on the dashboard without serving any strategic purpose.

This script identifies and (optionally) flattens orphaned remnants in
the local PAPER ledger. It does *not* touch live broker positions or
hit any external API. Default is a dry-run preview.

Filters (all configurable):
  --max-notional      $ ceiling for what counts as a remnant (default 25000)
  --max-loss-pct      Only flatten when |loss%| >= this (default 0.0 = any)
  --brokers           Comma-separated broker filter (default all)
  --symbols           Comma-separated symbol filter (default any)
  --apply             Actually write the close + tombstone rows
  --reason            Free-text reason recorded in instrument_metadata

Usage:
    python -m scripts.flatten_orphaned_remnants                 # dry-run all
    python -m scripts.flatten_orphaned_remnants --apply
    python -m scripts.flatten_orphaned_remnants --apply \
        --symbols AAPL,IWM,QQQ --max-notional 50000
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from system.local_paper_flatten import (  # noqa: E402
    LocalPaperFlattenPreview,
    LocalPaperFlattenResult,
    _preview,
    latest_open_local_paper_rows,
    normalize_broker_filter,
    refuse_live_local_paper_flatten,
)

load_dotenv()


def _wanted_symbols(raw: str | None) -> set[str]:
    if not raw:
        return set()
    return {x.strip().upper() for x in raw.split(",") if x.strip()}


def _row_loss_pct(row) -> Decimal:
    """Loss as a positive fraction (e.g. 0.012 = 1.2% loss). 0 for winners."""
    try:
        qty = Decimal(str(row.quantity or "0"))
        entry = Decimal(str(row.avg_entry_price or "0"))
        current = Decimal(str(row.current_price or row.avg_entry_price or "0"))
    except Exception:  # noqa: BLE001
        return Decimal("0")
    if qty == 0 or entry <= 0 or current <= 0:
        return Decimal("0")
    direction = Decimal(1) if qty > 0 else Decimal(-1)
    move = (current - entry) / entry
    pnl = direction * move
    return -pnl if pnl < 0 else Decimal("0")


async def _identify_remnants(
    *,
    session_factory,
    brokers: set[str],
    symbols: set[str],
    max_notional: Decimal,
    max_loss_pct: Decimal,
) -> tuple[list, list[LocalPaperFlattenPreview]]:
    rows = await latest_open_local_paper_rows(session_factory, brokers=brokers)
    matched_rows: list = []
    matched_previews: list[LocalPaperFlattenPreview] = []
    for row in rows:
        prev = _preview(row)
        if symbols and prev.symbol not in symbols:
            continue
        if prev.notional > max_notional:
            continue
        if max_loss_pct > 0:
            if _row_loss_pct(row) < max_loss_pct:
                continue
        matched_rows.append(row)
        matched_previews.append(prev)
    return matched_rows, matched_previews


async def _flatten_remnants(
    *,
    apply: bool,
    brokers: set[str],
    symbols: set[str],
    max_notional: Decimal,
    max_loss_pct: Decimal,
    reason: str,
) -> LocalPaperFlattenResult:
    refuse_live_local_paper_flatten()
    from storage.db import dispose_engine, init_async_database
    from storage.models import OrderLog, PositionLog

    engine, session_factory = await init_async_database()
    if session_factory is None:
        raise RuntimeError("database unavailable")
    try:
        rows, previews = await _identify_remnants(
            session_factory=session_factory,
            brokers=brokers,
            symbols=symbols,
            max_notional=max_notional,
            max_loss_pct=max_loss_pct,
        )
        if not apply or not rows:
            return LocalPaperFlattenResult(previews=previews, applied=False)

        now = datetime.now(timezone.utc)
        async with session_factory() as session:
            for row, item in zip(rows, previews, strict=True):
                signal_id = str(uuid.uuid4())
                order_id = str(uuid.uuid4())
                session.add(
                    OrderLog(
                        id=order_id,
                        broker_order_id=f"paper-remnant-flatten-{order_id[:12]}",
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
                        instrument_metadata={
                            "reduce_only": True,
                            "close_only": True,
                            "flatten_all": True,
                            "flatten_reason": reason,
                            "source_position_id": row.id,
                            "max_notional_filter": str(max_notional),
                            "max_loss_pct_filter": str(max_loss_pct),
                            "remnant_notional": str(item.notional),
                        },
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
            await session.commit()
        return LocalPaperFlattenResult(previews=previews, applied=True)
    finally:
        if engine is not None:
            await dispose_engine(engine)


def _print_previews(previews, applied: bool, action: str) -> None:
    if not previews:
        print(f"flatten_orphaned_remnants | {action}: no remnants matched filters")
        return
    print(
        f"flatten_orphaned_remnants | {action}: {len(previews)} position(s) "
        f"({'APPLIED' if applied else 'DRY-RUN'})"
    )
    total_notional = Decimal("0")
    for p in previews:
        total_notional += p.notional
        print(
            f"  {p.broker:>8} {p.symbol:<8} {p.side:>4} qty={p.quantity:>12} "
            f"price={p.price:>10} notional={p.notional:>12}"
        )
    print(f"  TOTAL remnant notional cleared: {total_notional}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply", action="store_true", help="write close + tombstone rows (default is dry-run)"
    )
    parser.add_argument(
        "--max-notional",
        type=str,
        default="25000",
        help="ceiling notional in USD (default 25000)",
    )
    parser.add_argument(
        "--max-loss-pct",
        type=str,
        default="0.0",
        help="only flatten when |loss%%| >= this fraction (default 0.0 = no filter)",
    )
    parser.add_argument(
        "--brokers",
        type=str,
        default="",
        help="comma-separated broker filter (default all)",
    )
    parser.add_argument(
        "--symbols",
        type=str,
        default="",
        help="comma-separated symbol filter (default any)",
    )
    parser.add_argument(
        "--reason",
        type=str,
        default="orphaned_remnant_cleanup",
        help="recorded in OrderLog.instrument_metadata.flatten_reason",
    )
    args = parser.parse_args()

    try:
        max_notional = Decimal(args.max_notional)
        max_loss_pct = Decimal(args.max_loss_pct)
    except Exception as exc:  # noqa: BLE001
        print(f"invalid numeric argument: {exc}", file=sys.stderr)
        return 2

    result = asyncio.run(
        _flatten_remnants(
            apply=args.apply,
            brokers=normalize_broker_filter(args.brokers),
            symbols=_wanted_symbols(args.symbols),
            max_notional=max_notional,
            max_loss_pct=max_loss_pct,
            reason=args.reason,
        )
    )
    _print_previews(result.previews, result.applied, "flatten")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
