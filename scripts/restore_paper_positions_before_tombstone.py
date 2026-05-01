"""Restore paper ledger positions from the snapshot before a tombstone event.

Default mode is dry-run. Use ``--apply`` only after reviewing the planned rows.

Example:
  python scripts/restore_paper_positions_before_tombstone.py --broker ibkr --tombstone-ts 2026-05-01T12:23:17Z
  python scripts/restore_paper_positions_before_tombstone.py --broker ibkr --tombstone-ts 2026-05-01T12:23:17Z --apply
"""

from __future__ import annotations

import argparse
import asyncio
import os
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from dotenv import load_dotenv


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _parse_ts(raw: str) -> datetime:
    s = raw.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _fmt_decimal(value: object) -> str:
    return format(Decimal(str(value or 0)), "f")


async def _connect():
    import asyncpg

    load_dotenv(_repo_root() / ".env")
    return await asyncpg.connect(
        host=os.getenv("POSTGRES_HOST", "localhost"),
        port=int(os.getenv("POSTGRES_PORT", "5432")),
        user=os.getenv("POSTGRES_USER", "mytbot"),
        password=os.getenv("POSTGRES_PASSWORD", ""),
        database=os.getenv("POSTGRES_DB", "mytbot"),
        timeout=10,
    )


async def _main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--broker", required=True, help="Broker to restore, e.g. ibkr")
    parser.add_argument("--tombstone-ts", required=True, help="UTC timestamp at/near the tombstone event")
    parser.add_argument("--apply", action="store_true", help="Insert restore rows")
    args = parser.parse_args()

    broker = args.broker.strip().lower()
    tombstone_ts = _parse_ts(args.tombstone_ts)
    conn = await _connect()
    try:
        rows = await conn.fetch(
            """
            WITH before_rows AS (
              SELECT DISTINCT ON (broker, symbol)
                broker, symbol, quantity, avg_entry_price, current_price,
                unrealised_pnl, asset_class, instrument_metadata
              FROM positions
              WHERE broker = $1
                AND timestamp < $2
              ORDER BY broker, symbol, timestamp DESC, id DESC
            ),
            after_rows AS (
              SELECT DISTINCT ON (broker, symbol)
                broker, symbol, quantity
              FROM positions
              WHERE broker = $1
                AND timestamp >= $2
              ORDER BY broker, symbol, timestamp DESC, id DESC
            )
            SELECT b.*
            FROM before_rows b
            LEFT JOIN after_rows a
              ON a.broker = b.broker AND a.symbol = b.symbol
            WHERE abs(b.quantity) > 0.00000001
              AND abs(coalesce(a.quantity, 0)) <= 0.00000001
            ORDER BY b.symbol
            """,
            broker,
            tombstone_ts,
        )
        if not rows:
            print(f"No restorable rows found for broker={broker} before {tombstone_ts.isoformat()}")
            return 0

        exposure = sum(abs(Decimal(str(r["quantity"])) * Decimal(str(r["current_price"]))) for r in rows)
        print(f"broker={broker} tombstone_ts={tombstone_ts.isoformat()} dry_run={not args.apply}")
        print(f"rows_to_restore={len(rows)} exposure={_fmt_decimal(exposure)}")
        for r in rows:
            qty = Decimal(str(r["quantity"]))
            px = Decimal(str(r["current_price"]))
            print(f"  {r['symbol']:12s} qty={_fmt_decimal(qty):>18s} px={_fmt_decimal(px):>12s} mv={_fmt_decimal(abs(qty * px))}")

        if not args.apply:
            print("Dry run only. Re-run with --apply to insert restore rows.")
            return 0

        now = datetime.now(timezone.utc)
        async with conn.transaction():
            for r in rows:
                await conn.execute(
                    """
                    INSERT INTO positions (
                      timestamp, symbol, broker, quantity, avg_entry_price,
                      current_price, unrealised_pnl, asset_class, instrument_metadata
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                    """,
                    now,
                    r["symbol"],
                    r["broker"],
                    r["quantity"],
                    r["avg_entry_price"],
                    r["current_price"],
                    r["unrealised_pnl"],
                    r["asset_class"],
                    r["instrument_metadata"],
                )
        print(f"Inserted {len(rows)} restore rows at {now.isoformat()}")
        return 0
    finally:
        await conn.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
