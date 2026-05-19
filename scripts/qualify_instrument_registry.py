"""CLI — warm broker qualification caches from the D116 instrument registry.

Currently the only broker with a true qualification step is IBKR. For
other brokers this script is a no-op aside from refreshing the
availability table.

Example::

    python scripts/qualify_instrument_registry.py --broker=ibkr --limit=100

The script queries the registry for IBKR rows whose status is
``requires_qualification`` (or ``unknown``), batches them up to the
``--limit`` budget, calls IBKR's ``qualify_symbol`` for each, and
updates the per-broker availability table accordingly. Already
``available`` rows are skipped.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from loguru import logger

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from instruments.canonical import canonical_to_broker
from instruments.registry import (
    AvailabilityRow,
    list_broker_availability,
    upsert_broker_availability,
)
from storage.db import init_async_database


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Warm broker qualification cache from the registry (D116).")
    p.add_argument(
        "--broker",
        default="ibkr",
        help="Broker name (currently only 'ibkr' has a qualification step).",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=100,
        help="Maximum number of symbols to attempt per run.",
    )
    p.add_argument(
        "--include-unknown",
        action="store_true",
        help="Also include rows currently marked 'unknown' (default: only requires_qualification).",
    )
    return p.parse_args(argv)


async def _qualify_ibkr(session_factory, *, limit: int, include_unknown: bool) -> int:
    from brokers.ibkr.adapter import IBKRAdapter

    statuses = ["requires_qualification"]
    if include_unknown:
        statuses.append("unknown")
    rows = await list_broker_availability(session_factory, broker="ibkr", statuses=statuses)
    rows.sort(key=lambda r: (r.canonical_symbol,))
    if limit > 0:
        rows = rows[:limit]
    if not rows:
        logger.info("script | qualify | no IBKR rows pending qualification")
        return 0

    adapter = IBKRAdapter(
        host=os.getenv("IBKR_HOST", "127.0.0.1"),
        port=int(os.getenv("IBKR_PORT", "7497")),
        client_id=int(os.getenv("IBKR_CLIENT_ID", "1")),
        account_id=os.getenv("IBKR_ACCOUNT_ID", ""),
        paper_mode=str(os.getenv("APP_ENV", "paper")).strip().lower() != "live",
    )
    ok = await adapter.connect()
    if not ok:
        logger.warning("script | qualify | IBKR connect failed; nothing qualified")
        return 2

    counts: Counter[str] = Counter()
    updates: list[AvailabilityRow] = []
    try:
        for row in rows:
            broker_sym = row.broker_symbol or canonical_to_broker(row.canonical_symbol, "ibkr")
            if not broker_sym:
                counts["skipped_translation"] += 1
                continue
            try:
                rec = await adapter.qualify_symbol(broker_sym, None, force=True)
            except Exception as exc:  # noqa: BLE001
                logger.warning("script | qualify | {} -> exception {}", broker_sym, exc)
                counts["failed"] += 1
                continue
            counts[rec.status] += 1
            status = "available" if rec.is_qualified() else "requires_qualification"
            updates.append(
                AvailabilityRow(
                    canonical_symbol=row.canonical_symbol,
                    broker="ibkr",
                    broker_symbol=rec.broker_symbol or broker_sym,
                    status=status,
                    last_checked_at=datetime.now(timezone.utc),
                    last_available_at=datetime.now(timezone.utc) if rec.is_qualified() else None,
                    qualification_payload={
                        "con_id": rec.con_id,
                        "exchange": rec.exchange,
                        "currency": rec.currency,
                        "sec_type": rec.sec_type,
                        "primary_exchange": rec.primary_exchange,
                    },
                    last_error=rec.error,
                )
            )
        if updates:
            await upsert_broker_availability(session_factory, broker="ibkr", rows=updates)
    finally:
        try:
            await adapter.disconnect()
        except Exception:  # noqa: BLE001
            pass

    print(
        "qualified={qualified} failed={failed} skipped_translation={skipped} total_attempts={total}".format(
            qualified=counts["qualified"],
            failed=counts["failed"],
            skipped=counts["skipped_translation"],
            total=sum(counts.values()),
        )
    )
    return 0 if counts["failed"] == 0 else 1


async def amain(args: argparse.Namespace) -> int:
    engine, session_factory = await init_async_database()
    if session_factory is None:
        logger.error("script | qualify | database unavailable; aborting")
        return 3
    try:
        if args.broker.lower() == "ibkr":
            rc = await _qualify_ibkr(
                session_factory,
                limit=int(args.limit),
                include_unknown=bool(args.include_unknown),
            )
        else:
            logger.info("script | qualify | broker={} has no qualification step", args.broker)
            rc = 0
    finally:
        if engine is not None:
            try:
                await engine.dispose()
            except Exception:  # noqa: BLE001
                pass
    return rc


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    return asyncio.run(amain(args))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
