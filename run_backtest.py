#!/usr/bin/env python3
"""
Run M3 strategy backtests from feature_snapshots.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import math
from decimal import Decimal
from pathlib import Path
from typing import Any

import pandas as pd
import yaml
from dotenv import load_dotenv
from sqlalchemy import select

from backtest.harness import (
    run_backtest_on_features,
    run_purged_cv_backtest,
    run_walk_forward_backtest,
)
from backtest.labels import TripleBarrierSpec, train_meta_label_model, triple_barrier_labels
from backtest.validation import deflated_sharpe_ratio, pbo_from_path_scores
from signals.accumulator import SignalAccumulator
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


def _missing_research_deps(*, for_purged_cv: bool, for_meta_labeling: bool) -> list[str]:
    missing: list[str] = []
    if for_purged_cv and importlib.util.find_spec("timeseriescv.cross_validation") is None:
        missing.append("timeseriescv")
    # mlfinlab is not available in many Python 3.13 environments; meta-labeling has fallback.
    if for_meta_labeling and importlib.util.find_spec("mlfinlab") is None:
        missing.append("mlfinlab")
    return missing


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

        _se_cfg = cfg.get("signal_engine", {}) or {}
        _acc = SignalAccumulator() if bool(_se_cfg.get("use_signal_accumulator", False)) else None
        se = SignalEngine(_se_cfg, accumulator=_acc)
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
        elif args.purged_cv:
            missing = _missing_research_deps(
                for_purged_cv=True,
                for_meta_labeling=False,
            )
            if missing:
                if args.strict_research_deps:
                    print(
                        "Missing strict research dependencies for purged CV: "
                        + ", ".join(missing)
                    )
                    return 3
                print(
                    "Warning: missing research dependency "
                    + ", ".join(missing)
                    + " — using fallback splitter implementation."
                )
            p = run_purged_cv_backtest(
                symbol=args.symbol,
                features=df,
                strategy=strategy,
                signal_engine=se,
                starting_cash=starting_cash,
                fee_bps=fee_bps,
                slippage_bps=slippage_bps,
                n_splits=args.cv_splits,
                n_test_splits=args.cv_test_splits,
                embargo_bars=args.embargo_bars,
                max_hold_bars=max_hold_bars,
            )
            path_scores: list[float] = []
            for i, fr in enumerate(p.fold_results, start=1):
                print(
                    f"  fold#{i}: trades={fr.trades} win_rate={fr.win_rate:.2%} "
                    f"pnl={fr.net_pnl} equity={fr.final_equity}"
                )
                path_scores.append(float(fr.net_pnl))
            avg_win_rate = (
                sum(fr.win_rate for fr in p.fold_results) / p.folds if p.folds else 0.0
            )
            sharpe_proxy = (
                (sum(path_scores) / len(path_scores))
                / max(1e-9, math.sqrt(sum((x - (sum(path_scores) / len(path_scores))) ** 2 for x in path_scores) / max(1, len(path_scores) - 1)))
                if path_scores
                else 0.0
            )
            dsr = deflated_sharpe_ratio(
                sharpe_proxy,
                n_trials=max(1, p.folds),
                n_obs=max(2, len(path_scores)),
            )
            pbo = pbo_from_path_scores(path_scores)
            print(
                f"{args.strategy} | {args.symbol} {args.timeframe} | purged_cv "
                f"folds={p.folds} avg_win_rate={avg_win_rate:.2%} "
                f"pbo={pbo:.2%} dsr={dsr:.3f}"
            )
        elif args.meta_labeling:
            missing = _missing_research_deps(
                for_purged_cv=False,
                for_meta_labeling=True,
            )
            if missing:
                if args.strict_research_deps:
                    print(
                        "Missing strict research dependencies for meta-labeling: "
                        + ", ".join(missing)
                    )
                    return 3
                print(
                    "Warning: missing research dependency "
                    + ", ".join(missing)
                    + " — using in-repo fallback meta-labeling implementation."
                )
            labels = triple_barrier_labels(
                df["close"],
                TripleBarrierSpec(
                    pt_mult=args.tb_pt_mult,
                    sl_mult=args.tb_sl_mult,
                    max_horizon=args.tb_horizon,
                    vol_window=args.tb_vol_window,
                ),
            )
            # Build a compact feature frame (exclude OHLCV raw columns)
            x = df.drop(columns=[c for c in ["open", "high", "low", "close", "volume"] if c in df.columns], errors="ignore")
            model = train_meta_label_model(x, labels)
            take_rate = float((labels != 0).mean()) if len(labels) else 0.0
            if model is None:
                print(
                    f"{args.strategy} | {args.symbol} {args.timeframe} | "
                    f"meta_labeling unavailable | take_rate={take_rate:.2%}"
                )
            else:
                score = float(model.score(x.fillna(0.0), (labels != 0).astype(int)))
                print(
                    f"{args.strategy} | {args.symbol} {args.timeframe} | "
                    f"meta_labeling trained | in_sample_acc={score:.2%} take_rate={take_rate:.2%}"
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
    p.add_argument(
        "--purged-cv",
        action="store_true",
        help="Run purged time-series CV style validation",
    )
    p.add_argument("--cv-splits", type=int, default=6)
    p.add_argument("--cv-test-splits", type=int, default=2)
    p.add_argument("--embargo-bars", type=int, default=5)
    p.add_argument(
        "--meta-labeling",
        action="store_true",
        help="Run triple-barrier labeling + train simple meta-label model",
    )
    p.add_argument("--tb-pt-mult", type=float, default=2.0)
    p.add_argument("--tb-sl-mult", type=float, default=1.5)
    p.add_argument("--tb-horizon", type=int, default=10)
    p.add_argument("--tb-vol-window", type=int, default=20)
    p.add_argument(
        "--strict-research-deps",
        action="store_true",
        help="Fail if strict research libs (timeseriescv/mlfinlab) are unavailable",
    )
    args = p.parse_args()
    raise SystemExit(asyncio.run(_amain(args)))


if __name__ == "__main__":
    main()

