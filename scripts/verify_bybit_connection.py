"""
Smoke test for Bybit credentials and APIs used by funding / global-edge paths.

Loads BYBIT_* from .env. Does not place orders (paper_mode=True).

Run: python scripts/verify_bybit_connection.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
from loguru import logger

from brokers.bybit.adapter import BybitAdapter


def _env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


async def main() -> int:
    load_dotenv(ROOT / ".env")
    api_key = os.getenv("BYBIT_API_KEY", "").strip()
    api_secret = os.getenv("BYBIT_API_SECRET", "").strip()
    testnet = _env_bool("BYBIT_TESTNET", False)
    cat = (os.getenv("BYBIT_CATEGORY", "linear") or "linear").strip().lower()
    if cat not in ("spot", "linear"):
        logger.warning("verify_bybit | invalid BYBIT_CATEGORY={} — using linear", cat)
        cat = "linear"

    if not api_key or not api_secret:
        logger.error("verify_bybit | set BYBIT_API_KEY and BYBIT_API_SECRET in .env")
        return 1

    adapter = BybitAdapter(
        api_key=api_key,
        api_secret=api_secret,
        paper_mode=True,
        testnet=testnet,
        category=cat,
    )
    if not await adapter.connect():
        logger.error("verify_bybit | connect failed")
        return 1

    try:
        balances = await adapter.get_balance()
        logger.info("verify_bybit | wallet_balance_rows={}", len(balances))

        ob = await adapter.get_order_book("BTCUSDT", depth=5)
        logger.info(
            "verify_bybit | order_book BTCUSDT | bids={} asks={}",
            len(ob.bids),
            len(ob.asks),
        )
        if not ob.bids or not ob.asks:
            logger.error("verify_bybit | empty order book (wrong category or symbol?)")
            return 1

        lp = await adapter.get_last_price("BTCUSDT")
        logger.info("verify_bybit | last_price BTCUSDT={}", lp)

        if cat == "linear":
            snap = await adapter.fetch_funding_market_snapshot("BTCUSDT")
            if snap is None:
                logger.error("verify_bybit | fetch_funding_market_snapshot returned None")
                return 1
            logger.info(
                "verify_bybit | funding | rate={} mark={}",
                snap.get("funding_rate"),
                snap.get("mark_price"),
            )
        else:
            logger.warning(
                "verify_bybit | BYBIT_CATEGORY=spot — funding arb needs linear perps; "
                "set BYBIT_CATEGORY=linear for carry leg"
            )

        logger.info("verify_bybit | OK")
        return 0
    finally:
        await adapter.disconnect()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
