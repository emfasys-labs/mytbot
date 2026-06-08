"""
scripts/backfill_features.py
============================

Deep-backfill the ``feature_snapshots`` store so strategies can actually be
validated (D157 edge gate). The live M2 pipeline only ever fetches a short
incremental window (1h / 5d), so non-equity instruments (crypto, forex,
futures, pairs legs) accumulate just a few days of hourly history — far too
thin for any walk-forward backtest.

This command pulls the **full practical history yfinance allows** for a chosen
symbol set, across one or more timeframes, and upserts it through the exact
same fetch -> features -> validate -> Postgres path the live pipeline uses
(``data.pipeline.ingest_symbol_yfinance``). It is idempotent: the upsert key is
``(symbol, timeframe, bar_timestamp)``, so re-running only fills gaps and
refreshes the latest bars.

yfinance interval limits drive the default periods:
  * ``1d`` -> ``max``  (decades for liquid ETFs; from-inception for crypto)
  * ``1h`` -> ``730d`` (yfinance hard-caps intraday 1h history at ~730 days)

Usage:
  python scripts/backfill_features.py                       # config symbols, 1d=max + 1h=730d
  python scripts/backfill_features.py --symbols SPY,QQQ,BTC-USD
  python scripts/backfill_features.py --timeframes 1h=730d  # hourly only
  python scripts/backfill_features.py --from-universe --scope core,scan --max-symbols 150
  python scripts/backfill_features.py --dry-run             # print plan + current depth, fetch nothing

Decimal is used for all OHLCV values (handled inside the pipeline upsert).
The live schema is untouched; this only writes more rows.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import os
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
from loguru import logger
from sqlalchemy import func, select

from data.pipeline import ingest_symbol_yfinance, load_pipeline_config
from data.training_universe import load_training_universe_symbols
from storage.db import dispose_engine, init_async_database
from storage.models import FeatureSnapshot

# Default fetch period per interval. yfinance caps intraday 1h history at
# ~730 days; daily ("max") returns from-inception bars.
_DEFAULT_TIMEFRAME_PERIODS: dict[str, str] = {
    "1d": "max",
    "1h": "730d",
    "30m": "60d",
    "15m": "60d",
    "5m": "60d",
}

# Gap-validation interval + staleness window per timeframe. ``stale_after_days``
# is always supplied because ``ingest_symbol_yfinance`` treats any backfill as
# "daily" for the staleness check (it only affects a validation warning flag on
# the final bar, never correctness).
_TIMEFRAME_META: dict[str, dict[str, int]] = {
    "1d": {"expected_interval_seconds": 86400, "stale_after_days": 14},
    "1h": {"expected_interval_seconds": 3600, "stale_after_days": 30},
    "30m": {"expected_interval_seconds": 1800, "stale_after_days": 14},
    "15m": {"expected_interval_seconds": 900, "stale_after_days": 14},
    "5m": {"expected_interval_seconds": 300, "stale_after_days": 14},
}


def _parse_timeframes(spec: str | None) -> list[tuple[str, str]]:
    """Parse ``--timeframes`` like ``1d=max,1h=730d`` -> [("1d","max"),("1h","730d")]."""
    if not spec:
        return [("1d", _DEFAULT_TIMEFRAME_PERIODS["1d"]), ("1h", _DEFAULT_TIMEFRAME_PERIODS["1h"])]
    out: list[tuple[str, str]] = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "=" in part:
            tf, period = part.split("=", 1)
            tf, period = tf.strip(), period.strip()
        else:
            tf = part
            period = _DEFAULT_TIMEFRAME_PERIODS.get(tf, "max")
        if not tf:
            continue
        out.append((tf, period))
    return out


async def _depth(session_factory, symbols: list[str], timeframe: str) -> dict[str, int]:
    """Return ``{symbol: bar_count}`` for the given timeframe (0 when absent)."""
    counts = {s: 0 for s in symbols}
    async with session_factory() as session:
        q = await session.execute(
            select(FeatureSnapshot.symbol, func.count())
            .where(
                FeatureSnapshot.symbol.in_(symbols),
                FeatureSnapshot.timeframe == timeframe,
            )
            .group_by(FeatureSnapshot.symbol)
        )
        for sym, n in q.all():
            counts[sym] = int(n)
    return counts


def _section_for(timeframe: str, period: str) -> dict[str, Any]:
    meta = _TIMEFRAME_META.get(
        timeframe, {"expected_interval_seconds": 86400, "stale_after_days": 14}
    )
    return {"interval": timeframe, "period": period, **meta}


async def _amain(args: argparse.Namespace) -> int:
    load_dotenv()
    cfg = load_pipeline_config(args.config)

    if args.symbols:
        symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    elif args.from_universe:
        symbols = load_training_universe_symbols(
            tiers_path=args.universe_tiers_path,
            scope=args.scope,
            max_symbols=args.max_symbols,
            fallback_to_static=True,
        )
    else:
        symbols = [str(s).strip() for s in (cfg.get("symbols") or []) if str(s).strip()]
    if not symbols:
        logger.error("backfill | no symbols resolved | pass --symbols or configure data_pipeline.yaml")
        return 2

    timeframes = _parse_timeframes(args.timeframes)
    if not timeframes:
        logger.error("backfill | no timeframes parsed from --timeframes={}", args.timeframes)
        return 2

    logger.info(
        "backfill | plan | symbols={} timeframes={}",
        len(symbols),
        [f"{tf}={p}" for tf, p in timeframes],
    )

    engine, session_factory = await init_async_database()
    if session_factory is None:
        logger.error("backfill | no database | fix POSTGRES_* and ensure Postgres is up")
        return 1

    try:
        for tf, period in timeframes:
            before = await _depth(session_factory, symbols, tf)
            before_total = sum(before.values())
            before_present = sum(1 for v in before.values() if v > 0)
            logger.info(
                "backfill | {} | before | rows={} symbols_with_data={}/{}",
                tf,
                before_total,
                before_present,
                len(symbols),
            )

            if args.dry_run:
                # Show the thinnest symbols so the operator sees what needs filling.
                thin = sorted(before.items(), key=lambda kv: kv[1])[:15]
                print(f"\n[dry-run] {tf} (period={period}) — current depth, thinnest first:")
                for sym, n in thin:
                    print(f"  {sym:<12} {n:>7} bars")
                continue

            tf_cfg = copy.deepcopy(cfg)
            tf_cfg["backfill"] = _section_for(tf, period)

            for i, sym in enumerate(symbols, 1):
                try:
                    stat = await ingest_symbol_yfinance(
                        session_factory, tf_cfg, sym, backfill=True
                    )
                    logger.info(
                        "backfill | {} | {}/{} | {} | upserted={} bars_total={}",
                        tf,
                        i,
                        len(symbols),
                        sym,
                        stat["upserted"],
                        stat["bars_total"],
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("backfill | {} | {} | FAILED | {}", tf, sym, exc)

            after = await _depth(session_factory, symbols, tf)
            after_total = sum(after.values())
            after_present = sum(1 for v in after.values() if v > 0)
            gained = after_total - before_total
            logger.info(
                "backfill | {} | after | rows={} (+{}) symbols_with_data={}/{}",
                tf,
                after_total,
                gained,
                after_present,
                len(symbols),
            )
            print(
                f"\n=== {tf} backfill summary (period={period}) ===\n"
                f"  rows:    {before_total} -> {after_total}  (+{gained})\n"
                f"  symbols: {before_present} -> {after_present} / {len(symbols)} have data"
            )
        return 0
    finally:
        await dispose_engine(engine)


def main() -> int:
    p = argparse.ArgumentParser(
        description="Deep-backfill feature_snapshots history for strategy validation (D157)."
    )
    p.add_argument("--config", default=None, help="Path to data_pipeline.yaml")
    p.add_argument(
        "--symbols",
        default="",
        help="Comma-separated symbols; default = data_pipeline.yaml::symbols",
    )
    p.add_argument(
        "--from-universe",
        action="store_true",
        help="Use data/runtime/universe_tiers.json instead of config symbols",
    )
    p.add_argument("--scope", default="core,scan", help="Universe tiers (with --from-universe)")
    p.add_argument("--max-symbols", type=int, default=None, help="Cap symbols (with --from-universe)")
    p.add_argument("--universe-tiers-path", default=None, help="Override universe tiers JSON path")
    p.add_argument(
        "--timeframes",
        default=None,
        help="Comma list of TF[=period], e.g. '1d=max,1h=730d' (default). "
        "Period defaults: 1d=max, 1h=730d.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print plan + current per-symbol depth and exit (fetch nothing)",
    )
    args = p.parse_args()
    try:
        return asyncio.run(_amain(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
