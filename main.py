"""
main.py
========
Entry point for the mytbot trading system.

Usage:
    python main.py                  # Start in paper mode (default)
    APP_ENV=live python main.py     # Start in live mode (careful!)

The system reads APP_ENV from .env to determine paper vs live mode.
All broker adapters respect this setting automatically.
"""

import asyncio
import os
from dotenv import load_dotenv
from loguru import logger

load_dotenv()

APP_ENV = os.getenv("APP_ENV", "paper")
PAPER_MODE = APP_ENV != "live"


async def main():
    logger.info(f"mytbot starting — mode: {APP_ENV.upper()}")

    if not PAPER_MODE:
        logger.warning("=" * 60)
        logger.warning("LIVE MODE — real money will be at risk")
        logger.warning("=" * 60)

    # TODO M1: initialise brokers, data pipeline, risk engine, execution engine
    # TODO M3: start strategy engine
    # TODO M4: connect risk engine
    # TODO M5: start execution loop

    logger.info("System ready. Press Ctrl+C to stop.")

    try:
        while True:
            await asyncio.sleep(1)
    except KeyboardInterrupt:
        logger.info("Shutting down...")


if __name__ == "__main__":
    asyncio.run(main())
