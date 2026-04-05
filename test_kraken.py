"""
Manual integration test for KrakenAdapter (python-kraken-sdk).

Loads KRAKEN_* from .env. Kraken has no paper trading; ``paper_mode=True`` only
blocks sending live orders via the adapter — balances and market data still
use the real API when keys are set.

Run from repo root:

    python test_kraken.py

Optional env:
    KRAKEN_PAPER_MODE=false   # allow adapter.place_order to hit the exchange (careful)
    KRAKEN_TEST_PLACE_ORDER=1 # with paper_mode false, place a tiny market order (real trade)
"""

from __future__ import annotations

import asyncio
import os
import time
from decimal import Decimal

from dotenv import load_dotenv
from loguru import logger

from brokers.base import Order, OrderSide, OrderType
from brokers.kraken.adapter import KrakenAdapter

STREAM_SECONDS = 8.0
PRICE_SYMBOL = "BTC/USD"
# Minimum BTC volume on Kraken spot is typically 0.0001 XBT; keep tiny if placing live.
ORDER_QTY = Decimal("0.0001")


def _env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


async def main() -> None:
    load_dotenv()

    api_key = os.getenv("KRAKEN_API_KEY", "").strip()
    api_secret = os.getenv("KRAKEN_API_SECRET", "").strip()
    paper_mode = _env_bool("KRAKEN_PAPER_MODE", True)
    test_place = _env_bool("KRAKEN_TEST_PLACE_ORDER", False)

    if not api_key or not api_secret:
        logger.error(
            "test_kraken | set KRAKEN_API_KEY and KRAKEN_API_SECRET in .env "
            "(Query funds + market data permissions)."
        )
        return

    adapter = KrakenAdapter(
        api_key=api_key,
        api_secret=api_secret,
        paper_mode=paper_mode,
    )

    if not await adapter.connect():
        logger.error("test_kraken | connect failed")
        return

    try:
        balances = await adapter.get_balance()
        print("Balances (non-zero):")
        if not balances:
            print(
                "  (no non-zero **spot** balances — see INFO log above: if Kraken returned "
                "rows but all zero, the API is fine and your spot cash is empty; "
                "funds in futures/earn/staking do not appear here.)"
            )
        for b in balances:
            if b.total == 0 and b.reserved == 0:
                continue
            print(
                f"  {b.currency}: total={b.total} available={b.available} "
                f"reserved={b.reserved}"
            )

        last = await adapter.get_last_price(PRICE_SYMBOL)
        print(f"\nLast {PRICE_SYMBOL}: {last}")

        print(f"\nPolling stream (~{int(STREAM_SECONDS)}s, ~1s per tick)...")
        deadline = time.monotonic() + STREAM_SECONDS
        n = 0
        async for tick in adapter.stream_prices([PRICE_SYMBOL]):
            print(
                f"  {tick.timestamp} {tick.symbol} last={tick.price} "
                f"bid={tick.bid} ask={tick.ask}"
            )
            n += 1
            if time.monotonic() >= deadline:
                break
        logger.info("test_kraken | stream | ticks={}", n)

        print(
            f"\nAdapter paper_mode={paper_mode} "
            "(False required for real orders; Kraken has no separate paper URL)."
        )

        if test_place:
            if paper_mode:
                logger.warning(
                    "test_kraken | KRAKEN_TEST_PLACE_ORDER=1 ignored while KRAKEN_PAPER_MODE is true"
                )
            else:
                cid = f"test-kraken-{int(time.time())}"
                order = Order(
                    symbol=PRICE_SYMBOL,
                    side=OrderSide.BUY,
                    order_type=OrderType.MARKET,
                    quantity=ORDER_QTY,
                    client_order_id=cid,
                    time_in_force="IOC",
                )
                print(
                    f"\nPlacing LIVE market BUY {ORDER_QTY} {PRICE_SYMBOL} (IOC) — real funds..."
                )
                res = await adapter.place_order(order)
                print(
                    f"Order: id={res.broker_order_id!r} status={res.status.value} "
                    f"filled={res.filled_quantity}/{res.quantity}"
                )
        else:
            print("\nSkipping live order (set KRAKEN_TEST_PLACE_ORDER=1 and KRAKEN_PAPER_MODE=false to test).")

    finally:
        await adapter.disconnect()
        print("\nDisconnected.")


if __name__ == "__main__":
    asyncio.run(main())
