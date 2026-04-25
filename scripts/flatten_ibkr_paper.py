"""
scripts/flatten_ibkr_paper.py
=============================
Emergency flatten of the IBKR paper-trading account.

The audit on 2026-04-25 found that early-iteration sizing bugs left the IBKR
paper book with 54,861-share AXTA / CALX positions (~$1.6M+ each), plus a
long tail of stuck pending orders. While those positions sit on the book,
they:

  - dominate the portfolio's drawdown calc and trip drawdown_limit on every
    fresh signal;
  - bias the allocator toward IBKR every iteration;
  - create permanent dedup blocks for new (symbol, broker) signals.

This one-shot script:

  1. Cancels every open / pending order at IBKR.
  2. Submits a market order in the *opposite* direction for every IBKR
     position, with reduce_only metadata so the risk engine bypass kicks in.

Paper mode only. Refuses to run when ``APP_ENV=live``.

Usage:
    python -m scripts.flatten_ibkr_paper           # dry-run by default
    python -m scripts.flatten_ibkr_paper --apply   # actually submit closes
    python -m scripts.flatten_ibkr_paper --apply --symbols AXTA,CALX
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Iterable

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger("flatten_ibkr_paper")


def _refuse_if_live() -> None:
    env = (os.getenv("APP_ENV", "paper") or "paper").strip().lower()
    if env == "live":
        logger.error("APP_ENV=live — refusing to run flatten script. Set APP_ENV=paper.")
        sys.exit(2)


async def _build_adapter():
    from brokers.ibkr.adapter import IBKRAdapter

    cfg = {
        "host": os.getenv("IBKR_HOST", "127.0.0.1"),
        "port": int(os.getenv("IBKR_PORT", "7497")),
        "client_id": int(os.getenv("IBKR_CLIENT_ID", "1") or "1") + 999,
    }
    adapter = IBKRAdapter(paper_mode=True, **cfg)
    if not await adapter.connect():
        raise RuntimeError("IBKR connect failed")
    return adapter


async def _cancel_all(adapter) -> int:
    try:
        opens = await adapter.get_open_orders()
    except Exception as exc:  # noqa: BLE001
        logger.warning("get_open_orders failed: %s", exc)
        return 0
    cancelled = 0
    for o in opens:
        bid = getattr(o, "broker_order_id", None) or getattr(o, "id", None)
        if not bid:
            continue
        try:
            await adapter.cancel_order(bid)
            logger.info("cancelled order %s (%s)", bid, getattr(o, "symbol", "?"))
            cancelled += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("cancel %s failed: %s", bid, exc)
    return cancelled


async def _flatten_positions(
    adapter,
    *,
    symbol_filter: Iterable[str] | None,
    apply: bool,
) -> int:
    from brokers.base import Order, OrderSide, OrderType

    try:
        positions = await adapter.get_positions()
    except Exception as exc:  # noqa: BLE001
        logger.error("get_positions failed: %s", exc)
        return 0

    wanted = {s.strip().upper() for s in (symbol_filter or []) if s and s.strip()}
    closed = 0
    for p in positions:
        sym = str(getattr(p, "symbol", "") or "").strip().upper()
        if not sym:
            continue
        if wanted and sym not in wanted:
            continue
        try:
            qty = Decimal(str(getattr(p, "quantity", "0") or "0"))
        except Exception:  # noqa: BLE001
            qty = Decimal("0")
        if qty == 0:
            continue

        side = OrderSide.SELL if qty > 0 else OrderSide.BUY
        close_qty = abs(qty).quantize(Decimal("1"))  # IBKR rejects fractional equities
        if close_qty <= 0:
            continue

        order = Order(
            symbol=sym,
            side=side,
            order_type=OrderType.MARKET,
            quantity=close_qty,
            limit_price=None,
            client_order_id=str(uuid.uuid4()),
            instrument_metadata={
                "reduce_only": True,
                "flatten_paper": True,
                "issued_at": datetime.now(timezone.utc).isoformat(),
            },
        )

        if not apply:
            logger.info(
                "[DRY-RUN] would close %s | side=%s qty=%s (current qty=%s)",
                sym, side.value, close_qty, qty,
            )
            continue

        try:
            res = await adapter.place_order(order)
            logger.info(
                "submitted close | %s side=%s qty=%s | broker_order_id=%s status=%s",
                sym,
                side.value,
                close_qty,
                getattr(res, "broker_order_id", "?"),
                getattr(res, "status", "?"),
            )
            closed += 1
        except Exception as exc:  # noqa: BLE001
            logger.error("close order failed for %s: %s", sym, exc)
    return closed


async def _amain(args: argparse.Namespace) -> int:
    _refuse_if_live()
    adapter = await _build_adapter()
    try:
        if args.cancel_open:
            n = await _cancel_all(adapter)
            logger.info("cancelled %d open IBKR orders", n)
        symbols = None
        if args.symbols:
            symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
        n = await _flatten_positions(adapter, symbol_filter=symbols, apply=args.apply)
        logger.info("flatten complete | submitted=%d apply=%s", n, args.apply)
    finally:
        try:
            await adapter.disconnect()
        except Exception:  # noqa: BLE001
            pass
    return 0


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Flatten IBKR paper account positions")
    p.add_argument("--apply", action="store_true", help="actually submit closes (default: dry-run)")
    p.add_argument("--no-cancel-open", dest="cancel_open", action="store_false",
                   help="skip cancelling open orders before closing positions")
    p.add_argument("--symbols", type=str, default="",
                   help="comma-separated subset of symbols to flatten (default: all)")
    p.set_defaults(cancel_open=True)
    return p.parse_args(argv)


if __name__ == "__main__":
    args = _parse_args()
    raise SystemExit(asyncio.run(_amain(args)))
