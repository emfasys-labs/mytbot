#!/usr/bin/env python3
"""
Run M3 strategy backtests from feature_snapshots.
"""

from __future__ import annotations

import argparse
import asyncio
from decimal import Decimal
from pathlib import Path
from typing import Any

import pandas as pd
import yaml
from dotenv import load_dotenv
from sqlalchemy import select

from backtest.harness import run_backtest_on_features, run_walk_forward_backtest
from signals.engine import SignalEngine
from storage.db import dispose_engine, init_async_database
from storage.models import FeatureSnapshot
from strategies.mean_reversion import MeanReversionStrategy
from strategies.momentum import MomentumBreakoutStrategy


def load_strategy_config(path: str | None = None) -> dict[str, Any]:
    p = Path(path) if path else Path("config/strategies.yaml")
    with p.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def _rows_to_features_frame(rows: list[FeatureSnapshot]) -> pd.DataFrame:
    out: list[dict[str, Any]] = []
    for r in rows:
        row = {
            "timestamp": r.bar_timestamp,
            "open": float(r.open),
            "high": float(r.high),
            "low": float(r.low),
            "close": float(r.close),
            "volume": float(r.volume),
        }
        if isinstance(r.features, dict):
            row.update(r.features)
        out.append(row)
    if not out:
        return pd.DataFrame()
    df = pd.DataFrame(out).set_index("timestamp").sort_index()
    return df


async def _amain(args: argparse.Namespace) -> int:
    cfg = load_strategy_config(args.config)
    engine, session_factory = await init_async_database()
    if session_factory is None:
        print("No database connection. Check POSTGRES_*.")
        return 1
    try:
        async with session_factory() as session:
            q = await session.execute(
                select(FeatureSnapshot)
                .where(
                    FeatureSnapshot.symbol == args.symbol,
                    FeatureSnapshot.timeframe == args.timeframe,
                )
                .order_by(FeatureSnapshot.bar_timestamp.asc())
            )
            rows = list(q.scalars().all())
        df = _rows_to_features_frame(rows)
        if df.empty:
            print(f"No feature rows for {args.symbol} {args.timeframe}")
            return 2

        se = SignalEngine(cfg.get("signal_engine", {}))
        strat_cfg = cfg.get("strategies", {})
        if args.strategy == "momentum_breakout":
            strategy = MomentumBreakoutStrategy(strat_cfg.get("momentum_breakout", {}))
        else:
            strategy = MeanReversionStrategy(strat_cfg.get("mean_reversion", {}))

        bt_cfg = cfg.get("backtest", {})
        starting_cash = Decimal(str(bt_cfg.get("starting_cash", 100000)))
        fee_bps = Decimal(str(bt_cfg.get("fee_bps", 10)))
        slippage_bps = Decimal(str(bt_cfg.get("slippage_bps", 5)))
        max_hold_bars = int(bt_cfg.get("max_hold_bars", 20))

        if args.walk_forward:
            train_bars = int(bt_cfg.get("walk_forward_train_bars", 252))
            test_bars = int(bt_cfg.get("walk_forward_test_bars", 63))
            step_bars = int(bt_cfg.get("walk_forward_step_bars", 63))
            wf = run_walk_forward_backtest(
                symbol=args.symbol,
                features=df,
                strategy=strategy,
                signal_engine=se,
                starting_cash=starting_cash,
                fee_bps=fee_bps,
                slippage_bps=slippage_bps,
                train_bars=train_bars,
                test_bars=test_bars,
                step_bars=step_bars,
                max_hold_bars=max_hold_bars,
            )
            print(
                f"{args.strategy} | {args.symbol} {args.timeframe} | walk_forward "
                f"windows={wf.windows} trades={wf.total_trades} "
                f"avg_win_rate={wf.average_win_rate:.2%} agg_pnl={wf.aggregate_net_pnl}"
            )
            for i, wr in enumerate(wf.window_results, start=1):
                print(
                    f"  window#{i}: trades={wr.trades} wins={wr.wins} losses={wr.losses} "
                    f"win_rate={wr.win_rate:.2%} pnl={wr.net_pnl} equity={wr.final_equity}"
                )
        else:
            result = run_backtest_on_features(
                symbol=args.symbol,
                features=df,
                strategy=strategy,
                signal_engine=se,
                starting_cash=starting_cash,
                fee_bps=fee_bps,
                slippage_bps=slippage_bps,
                max_hold_bars=max_hold_bars,
            )
            print(
                f"{args.strategy} | {args.symbol} {args.timeframe} | "
                f"trades={result.trades} wins={result.wins} losses={result.losses} "
                f"win_rate={result.win_rate:.2%} pnl={result.net_pnl} equity={result.final_equity}"
            )
        return 0
    finally:
        await dispose_engine(engine)


def main() -> None:
    load_dotenv()
    p = argparse.ArgumentParser(description="Run strategy backtest from feature store")
    p.add_argument("--config", default=None, help="Path to config/strategies.yaml")
    p.add_argument("--symbol", default="SPY")
    p.add_argument("--timeframe", default="1d")
    p.add_argument(
        "--strategy",
        choices=["momentum_breakout", "mean_reversion"],
        default="momentum_breakout",
    )
    p.add_argument(
        "--walk-forward",
        action="store_true",
        help="Run walk-forward validation windows configured in strategies.yaml",
    )
    args = p.parse_args()
    raise SystemExit(asyncio.run(_amain(args)))


if __name__ == "__main__":
    main()

