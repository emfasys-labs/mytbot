"""
Manual integration test for AlpacaAdapter (alpaca-py).

Loads ALPACA_* from .env. Use paper keys with ALPACA_PAPER_MODE=true (default).

Set ALPACA_TEST_PLACE_ORDER=1 to submit a tiny fractional market order on paper (symbol ALPACA_TEST_SYMBOL, default AAPL).

Run: python test_alpaca.py   or   .\\test-alpaca.ps1
"""

from __future__ import annotations

import asyncio
import os
import time
from decimal import Decimal

from dotenv import load_dotenv
from loguru import logger

from brokers.base import Order, OrderSide, OrderType
from brokers.alpaca.adapter import AlpacaAdapter

STREAM_SECONDS = 8.0
DEFAULT_PRICE_SYMBOL = "AAPL"
ORDER_QTY = Decimal("0.01")


def _fmt_decimal(d: Decimal) -> str:
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

    api_key = os.getenv("ALPACA_API_KEY", "").strip()
    api_secret = os.getenv("ALPACA_API_SECRET", "").strip()
    paper_mode = _env_bool("ALPACA_PAPER_MODE", True)
    test_place = _env_bool("ALPACA_TEST_PLACE_ORDER", False)
    price_symbol = os.getenv("ALPACA_TEST_SYMBOL", DEFAULT_PRICE_SYMBOL).strip() or DEFAULT_PRICE_SYMBOL

    if not api_key or not api_secret:
        logger.error("test_alpaca | set ALPACA_API_KEY and ALPACA_API_SECRET in .env")
        return

    adapter = AlpacaAdapter(
        api_key=api_key,
        api_secret=api_secret,
        paper_mode=paper_mode,
    )

    if not await adapter.connect():
        logger.error("test_alpaca | connect failed")
        return

    try:
        balances = await adapter.get_balance()
        print("Balances:")
        for b in balances:
            print(
                f"  {b.currency}: total={_fmt_decimal(b.total)} "
                f"available={_fmt_decimal(b.available)} reserved={_fmt_decimal(b.reserved)}"
            )

        positions = await adapter.get_positions()
        print(f"\nOpen positions: {len(positions)}")
        for p in positions[:10]:
            print(
                f"  {p.symbol} qty={_fmt_decimal(p.quantity)} "
                f"avg={_fmt_decimal(p.avg_entry_price)} mtm={_fmt_decimal(p.current_price)}"
            )
        if len(positions) > 10:
            print("  ...")

        last = await adapter.get_last_price(price_symbol)
        print(f"\nLast {price_symbol}: {_fmt_decimal(last)}")

        candles = await adapter.get_candles(price_symbol, "1d", limit=5)
        print(f"\nDaily candles (last {len(candles)}):")
        for c in candles:
            print(
                f"  {c.timestamp} O={_fmt_decimal(c.open)} H={_fmt_decimal(c.high)} "
                f"L={_fmt_decimal(c.low)} C={_fmt_decimal(c.close)}"
            )

        print(f"\nPolling stream (~{int(STREAM_SECONDS)}s, ~1s per tick)...")
        deadline = time.monotonic() + STREAM_SECONDS
        n = 0
        async for tick in adapter.stream_prices([price_symbol]):
            bid_s = _fmt_decimal(tick.bid) if tick.bid is not None else "None"
            ask_s = _fmt_decimal(tick.ask) if tick.ask is not None else "None"
            print(
                f"  {tick.timestamp} {tick.symbol} last={_fmt_decimal(tick.price)} "
                f"bid={bid_s} ask={ask_s}"
            )
            n += 1
            if time.monotonic() >= deadline:
                break
        logger.info("test_alpaca | stream | ticks={}", n)

        ac = await adapter.get_asset_class(price_symbol)
        print(f"\nAsset class for {price_symbol}: {ac.value}")

        print(f"\nAdapter paper_mode={paper_mode} (use paper keys when true).")

        if test_place:
            cid = f"test-alpaca-{int(time.time())}"
            order = Order(
                symbol=price_symbol,
                side=OrderSide.BUY,
                order_type=OrderType.MARKET,
                quantity=ORDER_QTY,
                client_order_id=cid,
                time_in_force="DAY",
            )
            res = await adapter.place_order(order)
            print(
                f"\nTest order: id={res.broker_order_id} status={res.status.value} "
                f"filled={_fmt_decimal(res.filled_quantity)}"
            )

    finally:
        await adapter.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
