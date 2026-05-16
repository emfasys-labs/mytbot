#!/usr/bin/env python3
"""
M2 data pipeline entrypoint: yfinance OHLCV + features, NewsAPI, FRED.

Examples:
  python run_pipeline.py              # one incremental pass (default)
  python run_pipeline.py --once       # same as above (explicit)
  python run_pipeline.py --backfill   # ~2y daily bars per config/data_pipeline.yaml
  python run_pipeline.py --loop       # hourly loop (see loop_interval_seconds)
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from dotenv import load_dotenv
from loguru import logger

from control.startup_validation import validate_startup_env
from data.pipeline import load_pipeline_config, run_loop, run_once
from data.training_universe import load_training_universe_symbols
from storage.db import dispose_engine, init_async_database


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="M2 data pipeline")
    p.add_argument("--config", default=None, help="Path to data_pipeline.yaml")
    p.add_argument(
        "--symbols",
        default=None,
        help="Comma-separated symbol override (e.g. SPY,QQQ,BTC-USD)",
    )
    p.add_argument(
        "--symbols-from-universe",
        action="store_true",
        help="Use data/runtime/universe_tiers.json instead of config symbols",
    )
    p.add_argument(
        "--universe-scope",
        default="core,scan",
        help="Universe tiers to use: core, scan, light, all, or comma-separated combinations",
    )
    p.add_argument(
        "--universe-tiers-path",
        default=None,
        help="Override universe tiers JSON path; defaults to data/runtime/universe_tiers.json",
    )
    p.add_argument(
        "--max-symbols",
        type=int,
        default=None,
        help="Cap symbols selected from the universe",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print selected universe symbols and exit before opening the database",
    )
    mode = p.add_mutually_exclusive_group()
    mode.add_argument(
        "--loop",
        action="store_true",
        help="Run incremental ingest on loop_interval_seconds forever",
    )
    mode.add_argument(
        "--once",
        action="store_true",
        help="Single pass (incremental unless --backfill) then exit; default when --loop is not used",
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
    p.add_argument(
        "--training-backfill",
        action="store_true",
        help="Use historical_training_backfill config, select universe symbols, and skip news/FRED",
    )
    p.add_argument("--skip-news", action="store_true", help="Skip news ingestion for this run")
    p.add_argument("--skip-fred", action="store_true", help="Skip FRED ingestion for this run")
    args = p.parse_args()
    if args.training_backfill:
        args.symbols_from_universe = True
    if args.backfill_first and not args.loop:
        logger.warning("run_pipeline | --backfill-first ignored without --loop")
    return args


async def _amain(args: argparse.Namespace) -> int:
    cfg = load_pipeline_config(args.config)
    if args.training_backfill:
        section = cfg.get("historical_training_backfill") or {}
        if not section:
            logger.error("run_pipeline | --training-backfill requested but historical_training_backfill is missing")
            return 2
        cfg["backfill"] = dict(section)
        args.backfill = True
        args.skip_news = True
        args.skip_fred = True
        logger.info("run_pipeline | training backfill section | {}", cfg["backfill"])
    if args.symbols:
        symbols = [s.strip() for s in str(args.symbols).split(",") if s.strip()]
        if not symbols:
            logger.error("run_pipeline | --symbols provided but no valid symbols parsed")
            return 2
        cfg["symbols"] = symbols
        logger.info("run_pipeline | symbols override | {}", symbols)
    elif args.symbols_from_universe:
        symbols = load_training_universe_symbols(
            tiers_path=args.universe_tiers_path,
            scope=args.universe_scope,
            max_symbols=args.max_symbols,
            fallback_to_static=True,
        )
        if not symbols:
            logger.error("run_pipeline | universe selection returned no symbols")
            return 2
        cfg["symbols"] = symbols
        logger.info(
            "run_pipeline | universe symbols | scope={} count={} first={}",
            args.universe_scope,
            len(symbols),
            symbols[:10],
        )
        if args.dry_run:
            print("\n".join(symbols))
            return 0
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
            await run_once(
                session_factory,
                cfg,
                backfill=args.backfill,
                include_news=not args.skip_news,
                include_fred=not args.skip_fred,
            )
        return 0
    finally:
        await dispose_engine(engine)


def main() -> None:
    load_dotenv()
    args = _parse_args()
    validate_startup_env(component="run_pipeline.py", require_postgres=not args.dry_run, strict=True)
    try:
        code = asyncio.run(_amain(args))
    except KeyboardInterrupt:
        code = 130
    sys.exit(code)


if __name__ == "__main__":
    main()
