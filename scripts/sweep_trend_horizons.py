"""
scripts/sweep_trend_horizons.py
===============================

Validate multi-horizon trend snipers (D158 Phase 3).

A trend weapon's HORIZON is set by its lookback + holding period, not the bar
timeframe: a 150-day Donchian breakout held for months is a "monthly sniper"
even on daily bars. Time-series momentum is strongest at long (3–12 month)
horizons — the most robust anomaly in finance — so longer-lookback trend
should have *stronger* edge than the 50-day swing variant.

This sweeps entry_lookback (trend_breakout) and fast/slow MA (trend_following)
across horizon bands, each with a holding cap matched to its horizon, on the
backfilled daily data, and reports out-of-sample post-cost edge. Read-only.

  python scripts/sweep_trend_horizons.py --timeframe 1d
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from decimal import Decimal
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import yaml
from dotenv import load_dotenv
from sqlalchemy import select

from backtest.edge_gate import aggregate_walk_forward
from backtest.harness import run_walk_forward_backtest
from signals.engine import SignalEngine
from storage.db import init_async_database
from storage.models import FeatureSnapshot
from strategies.trend_breakout import TrendBreakoutStrategy
from strategies.trend_following import TrendFollowingStrategy


def _rows_to_frame(rows: list[FeatureSnapshot]) -> pd.DataFrame:
    out: list[dict[str, Any]] = []
    for r in rows:
        row = {"timestamp": r.bar_timestamp, "open": float(r.open), "high": float(r.high),
               "low": float(r.low), "close": float(r.close), "volume": float(r.volume)}
        if isinstance(r.features, dict):
            row.update(r.features)
        out.append(row)
    return pd.DataFrame(out).set_index("timestamp").sort_index() if out else pd.DataFrame()


# (label, strategy_class, config_overrides, max_hold_bars) — hold matched to horizon.
def _variants() -> list[tuple[str, Any, dict[str, Any], int]]:
    base_b = {"enabled": True, "atr_lookback": 20, "min_breakout_atr": 0.5, "base_target_notional": "20000"}
    base_f = {"enabled": True, "base_target_notional": "20000"}
    return [
        # trend_breakout — swing → position → long sniper
        ("breakout L50  (swing)",    TrendBreakoutStrategy, {**base_b, "entry_lookback": 50},  20),
        ("breakout L100 (weekly)",   TrendBreakoutStrategy, {**base_b, "entry_lookback": 100}, 40),
        ("breakout L150 (monthly)",  TrendBreakoutStrategy, {**base_b, "entry_lookback": 150}, 60),
        ("breakout L200 (position)", TrendBreakoutStrategy, {**base_b, "entry_lookback": 200}, 90),
        # trend_following — fast → slow → very slow
        ("trend MA20/50  (swing)",   TrendFollowingStrategy, {**base_f, "fast_period": 20, "slow_period": 50},  20),
        ("trend MA50/100 (weekly)",  TrendFollowingStrategy, {**base_f, "fast_period": 50, "slow_period": 100}, 40),
        ("trend MA50/200 (monthly)", TrendFollowingStrategy, {**base_f, "fast_period": 50, "slow_period": 200}, 90),
    ]


async def _amain(args: argparse.Namespace) -> int:
    load_dotenv()
    cfg = yaml.safe_load(open("config/strategies.yaml", encoding="utf-8")) or {}
    bt = cfg.get("backtest", {}) or {}
    starting_cash = Decimal(str(bt.get("starting_cash", 100000)))
    fee_bps = Decimal(str(bt.get("fee_bps", 10)))
    slippage_bps = Decimal(str(bt.get("slippage_bps", 5)))
    train_bars = int(bt.get("walk_forward_train_bars", 252))
    test_bars = int(bt.get("walk_forward_test_bars", 63))
    step_bars = int(bt.get("walk_forward_step_bars", 63))
    se_cfg = cfg.get("signal_engine", {}) or {}

    pipe = yaml.safe_load(open("config/data_pipeline.yaml", encoding="utf-8")) or {}
    symbols = [str(s).strip() for s in (pipe.get("symbols") or []) if str(s).strip()]

    engine, sf = await init_async_database()
    if sf is None:
        print("No DB."); return 1
    features: dict[str, pd.DataFrame] = {}
    async with sf() as s:
        for sym in symbols:
            q = await s.execute(
                select(FeatureSnapshot)
                .where(FeatureSnapshot.symbol == sym, FeatureSnapshot.timeframe == args.timeframe)
                .order_by(FeatureSnapshot.bar_timestamp.asc())
            )
            df = _rows_to_frame(list(q.scalars().all()))
            if not df.empty:
                features[sym] = df

    print(f"\ntrend horizon sweep  tf={args.timeframe}  symbols={len(features)}  "
          f"fee_bps={fee_bps} slip_bps={slippage_bps}\n" + "=" * 96)
    print(f"{'variant':<28}{'hold':>6}{'trades':>8}{'net_pnl':>14}{'exp/trade':>12}{'consist':>9}{'pf':>8}{'win%':>8}")
    print("-" * 96)
    for label, cls, vcfg, max_hold in _variants():
        strat = cls(vcfg)
        se = SignalEngine(se_cfg, accumulator=None)
        windows: list[Any] = []
        used = 0
        for sym, df in features.items():
            if len(df) < train_bars + test_bars:
                continue
            wf = run_walk_forward_backtest(
                symbol=sym, features=df, strategy=strat, signal_engine=se,
                starting_cash=starting_cash, fee_bps=fee_bps, slippage_bps=slippage_bps,
                train_bars=train_bars, test_bars=test_bars, step_bars=step_bars,
                max_hold_bars=max_hold,
            )
            if wf.window_results:
                windows.extend(wf.window_results)
                used += 1
        m = aggregate_walk_forward(label, windows, symbols_evaluated=used)
        print(f"{label:<28}{max_hold:>6}{m.total_trades:>8}{float(m.total_net_pnl):>14.0f}"
              f"{float(m.expectancy_per_trade):>12.2f}{float(m.consistency):>9.2f}"
              f"{float(m.profit_factor):>8.2f}{m.avg_win_rate*100:>7.1f}%")
    print("-" * 96)
    if engine is not None:
        await engine.dispose()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--timeframe", default="1d")
    return asyncio.run(_amain(ap.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
