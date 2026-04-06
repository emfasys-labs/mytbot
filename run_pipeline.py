#!/usr/bin/env python3
"""
M2 data pipeline entrypoint: yfinance OHLCV + features, NewsAPI, FRED.

Examples:
  python run_pipeline.py              # one incremental pass (default)
  python run_pipeline.py --backfill   # ~2y daily bars per config/data_pipeline.yaml
  python run_pipeline.py --loop       # hourly loop (see loop_interval_seconds)
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from dotenv import load_dotenv
from loguru import logger

from data.pipeline import load_pipeline_config, run_loop, run_once
from storage.db import dispose_engine, init_async_database


async def _amain(args: argparse.Namespace) -> int:
    cfg = load_pipeline_config(args.config)
    if args.symbols:
        symbols = [s.strip() for s in str(args.symbols).split(",") if s.strip()]
        if not symbols:
            logger.error("run_pipeline | --symbols provided but no valid symbols parsed")
            return 2
        cfg["symbols"] = symbols
        logger.info("run_pipeline | symbols override | {}", symbols)
    engine, session_factory = await init_async_database()
    if session_factory is None:
        logger.error("run_pipeline | no database | fix POSTGRES_* and ensure Postgres is up")
        return 1
    try:
        if args.loop:
            await run_loop(
                session_factory,
                cfg,
                backfill_first=args.backfill_first,
            )
        else:
            await run_once(session_factory, cfg, backfill=args.backfill)
        return 0
    finally:
        await dispose_engine(engine)


def main() -> None:
    load_dotenv()
    p = argparse.ArgumentParser(description="M2 data pipeline")
    p.add_argument("--config", default=None, help="Path to data_pipeline.yaml")
    p.add_argument(
        "--symbols",
        default=None,
        help="Comma-separated symbol override (e.g. SPY,QQQ,BTC-USD)",
    )
    g = p.add_mutually_exclusive_group()
    g.add_argument(
        "--loop",
        action="store_true",
        help="Run incremental ingest on loop_interval_seconds forever",
    )
    p.add_argument(
        "--backfill",
        action="store_true",
        help="Use backfill interval/period (e.g. 2y 1d) instead of incremental",
    )
    p.add_argument(
        "--backfill-first",
        action="store_true",
        help="With --loop: run one backfill pass before entering the loop",
    )
    args = p.parse_args()
    if args.backfill_first and not args.loop:
        logger.warning("run_pipeline | --backfill-first ignored without --loop")
    try:
        code = asyncio.run(_amain(args))
    except KeyboardInterrupt:
        code = 130
    sys.exit(code)


if __name__ == "__main__":
    main()
