"""
Manual integration test for BinanceAdapter (python-binance).

Loads BINANCE_* from .env. ``paper_mode=True`` (default) does not place orders;
set BINANCE_PAPER_MODE=false and BINANCE_TEST_PLACE_ORDER=1 for a tiny live order.

Use BINANCE_TESTNET=true with keys from https://testnet.binance.vision/ for sandbox.

Run: python test_binance.py   or   .\\test-binance.ps1
"""

from __future__ import annotations

import asyncio
import os
import time
from decimal import Decimal

from dotenv import load_dotenv
from loguru import logger

from brokers.base import Order, OrderSide, OrderType
from brokers.binance.adapter import BinanceAdapter

STREAM_SECONDS = 8.0
PRICE_SYMBOL = "BTC/USDT"
ORDER_QTY = Decimal("0.0001")


def _fmt_decimal(d: Decimal) -> str:
    """Human-readable decimal (avoid scientific notation in the console)."""
    s = format(d, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s if s else "0"


def _env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


async def main() -> None:
    load_dotenv()

    api_key = os.getenv("BINANCE_API_KEY", "").strip()
    api_secret = os.getenv("BINANCE_API_SECRET", "").strip()
    paper_mode = _env_bool("BINANCE_PAPER_MODE", True)
    test_place = _env_bool("BINANCE_TEST_PLACE_ORDER", False)
    testnet = _env_bool("BINANCE_TESTNET", False)
    tld = os.getenv("BINANCE_TLD", "com").strip() or "com"

    if not api_key or not api_secret:
        logger.error("test_binance | set BINANCE_API_KEY and BINANCE_API_SECRET in .env")
        return

    adapter = BinanceAdapter(
        api_key=api_key,
        api_secret=api_secret,
        paper_mode=paper_mode,
        testnet=testnet,
        tld=tld,
    )

    if not await adapter.connect():
        logger.error("test_binance | connect failed")
        return

    try:
        balances = await adapter.get_balance()
        print("Balances (non-zero):")
        if not balances:
            print(
                "  (no non-zero balances — new account or all on sub-accounts/margin; "
                "see INFO log. testnet: fund from faucet on testnet.binance.vision.)"
            )
        for b in balances:
            print(
                f"  {b.currency}: total={_fmt_decimal(b.total)} "
                f"available={_fmt_decimal(b.available)} reserved={_fmt_decimal(b.reserved)}"
            )

        last = await adapter.get_last_price(PRICE_SYMBOL)
        print(f"\nLast {PRICE_SYMBOL}: {_fmt_decimal(last)}")

        print(f"\nPolling stream (~{int(STREAM_SECONDS)}s, ~1s per tick)...")
        deadline = time.monotonic() + STREAM_SECONDS
        n = 0
        async for tick in adapter.stream_prices([PRICE_SYMBOL]):
            bid_s = _fmt_decimal(tick.bid) if tick.bid is not None else "None"
            ask_s = _fmt_decimal(tick.ask) if tick.ask is not None else "None"
            print(
                f"  {tick.timestamp} {tick.symbol} last={_fmt_decimal(tick.price)} "
                f"bid={bid_s} ask={ask_s}"
            )
            n += 1
            if time.monotonic() >= deadline:
                break
        logger.info("test_binance | stream | ticks={}", n)

        print(
            f"\nAdapter paper_mode={paper_mode} testnet={testnet} tld={tld} "
            "(live orders need paper_mode=false)."
        )

        if test_place:
            if paper_mode:
                logger.warning(
                    "test_binance | BINANCE_TEST_PLACE_ORDER=1 ignored while BINANCE_PAPER_MODE is true"
                )
            else:
                cid = f"test-binance-{int(time.time())}"
                order = Order(
                    symbol=PRICE_SYMBOL,
                    side=OrderSide.BUY,
                    order_type=OrderType.MARKET,
                    quantity=ORDER_QTY,
                    client_order_id=cid,
                    time_in_force="IOC",
                )
                print(
                    f"\nPlacing MARKET BUY {ORDER_QTY} {PRICE_SYMBOL} — "
                    f"{'TESTNET' if testnet else 'LIVE'} — uses real (test) balance..."
                )
                res = await adapter.place_order(order)
                print(
                    f"Order: id={res.broker_order_id!r} status={res.status.value} "
                    f"filled={res.filled_quantity}/{res.quantity}"
                )
        else:
            print(
                "\nSkipping order (set BINANCE_TEST_PLACE_ORDER=1 and BINANCE_PAPER_MODE=false to test)."
            )

    finally:
        await adapter.disconnect()
        print("\nDisconnected.")


if __name__ == "__main__":
    asyncio.run(main())
