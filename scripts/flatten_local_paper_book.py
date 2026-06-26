"""
Flatten the local paper ledger by writing filled close rows and tombstones.

This is for APP_ENV=paper only. It does not submit broker orders. It repairs
the simulated PositionLog book when the normal zero-allocation trading-loop
path is unavailable or wedged.

Usage:
    python -m scripts.flatten_local_paper_book
    python -m scripts.flatten_local_paper_book --apply
    python -m scripts.flatten_local_paper_book --apply --brokers ibkr,alpaca
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from decimal import Decimal
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from system.local_paper_flatten import (  # noqa: E402
    flatten_local_paper_book,
    normalize_broker_filter,
    normalize_symbol_filter,
    refuse_live_local_paper_flatten,
)

load_dotenv()


def _wanted(raw: str) -> set[str]:
    return normalize_broker_filter(raw)


def _wanted_symbols(raw: str) -> set[str]:
    return normalize_symbol_filter(raw)


async def _flatten(*, apply: bool, brokers: set[str], symbols: set[str]) -> int:
    try:
        refuse_live_local_paper_flatten()
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc
    result = await flatten_local_paper_book(
        apply=apply,
        brokers=brokers,
        symbols=symbols,
        reason="local_paper_book_repair",
    )
    if not result.previews:
        print("local paper book already flat")
        return 0

    for row in result.previews:
        print(
            f"{'[APPLY]' if apply else '[DRY-RUN]'} close "
            f"{row.broker}:{row.symbol} side={row.side} qty={row.quantity} "
            f"price={row.price} notional={row.notional.quantize(Decimal('0.01'))}"
        )
    if apply:
        print(f"flattened {result.count} local paper position(s)")
    else:
        print(f"dry run only; {result.count} local paper position(s) would be flattened")
    return 0


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Flatten local paper PositionLog book")
    p.add_argument("--apply", action="store_true", help="write close rows and tombstones")
    p.add_argument("--brokers", default="", help="comma-separated broker filter")
    p.add_argument("--symbols", default="", help="comma-separated symbol filter")
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    raise SystemExit(asyncio.run(_flatten(
        apply=args.apply,
        brokers=_wanted(args.brokers),
        symbols=_wanted_symbols(args.symbols),
    )))
