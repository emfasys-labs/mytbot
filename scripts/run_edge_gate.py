"""
scripts/run_edge_gate.py
========================

Evaluate each strategy's OUT-OF-SAMPLE post-cost expectancy across the symbol
universe and write per-strategy verdicts to the edge-gate registry (D157).

The live trading loop reads that registry and gates capital: ``blocked``
strategies get none, ``reduced`` strategies are down-weighted. Run this on a
schedule (e.g. nightly) and after any strategy change.

Usage:
  python scripts/run_edge_gate.py                 # all enabled per-symbol strategies, universe from config
  python scripts/run_edge_gate.py --symbols AAPL,MSFT,SPY
  python scripts/run_edge_gate.py --dry-run       # print table, do not write registry
  python scripts/run_edge_gate.py --timeframe 1h

Cost model is the backtest block in config/strategies.yaml (fee_bps /
slippage_bps). Set these to realistic LIVE costs so the gate proves edge
survives reality, not optimistic paper costs.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import yaml
from dotenv import load_dotenv
from sqlalchemy import select

from backtest.edge_gate import (
    DEFAULT_REGISTRY_PATH,
    EdgeGateRegistry,
    EdgeGateThresholds,
    aggregate_walk_forward,
    decide_verdict,
)
from backtest.harness import run_walk_forward_backtest
from signals.accumulator import SignalAccumulator
from signals.engine import SignalEngine
from storage.db import init_async_database
from storage.models import FeatureSnapshot

# Strategies that operate on a single symbol's OHLCV/feature frame and so can
# be evaluated by the per-symbol walk-forward harness. Cross-sectional
# strategies (pairs, regime rotation, stat-arb) cannot be measured here; they
# fall through to the gate's ``unproven_policy``.
from strategies.momentum import MomentumBreakoutStrategy
from strategies.mean_reversion import MeanReversionStrategy
from strategies.volume_flow import VolumeFlowStrategy
from strategies.volatility_regime import VolatilityRegimeStrategy
from strategies.trend_breakout import TrendBreakoutStrategy
from strategies.trend_following import TrendFollowingStrategy

# Only strategies that expose ``generate_signal(symbol, ohlcv_df)`` can be
# measured by the per-symbol harness. News/event/cross-sectional strategies
# (event_driven_news, pairs_trading, regime_rotation, stat_arb) use other
# interfaces and fall through to the gate's ``unproven_policy`` in the loop.
_PER_SYMBOL_STRATEGIES: dict[str, tuple[type, str]] = {
    "momentum_breakout": (MomentumBreakoutStrategy, "momentum_breakout"),
    "mean_reversion": (MeanReversionStrategy, "mean_reversion"),
    "volume_flow": (VolumeFlowStrategy, "volume_flow"),
    "volatility_regime": (VolatilityRegimeStrategy, "volatility_regime"),
    "trend_breakout": (TrendBreakoutStrategy, "trend_breakout"),       # D158 sniper
    "trend_following": (TrendFollowingStrategy, "trend_following"),     # D158 shotgun
}


def _load_yaml(path: str) -> dict[str, Any]:
    p = Path(path)
    if not p.exists():
        return {}
    return yaml.safe_load(p.read_text(encoding="utf-8")) or {}


def _rows_to_frame(rows: list[FeatureSnapshot]) -> pd.DataFrame:
    out: list[dict[str, Any]] = []
    for r in rows:
        row = {
            "timestamp": r.bar_timestamp,
            "open": float(r.open), "high": float(r.high), "low": float(r.low),
            "close": float(r.close), "volume": float(r.volume),
        }
        if isinstance(r.features, dict):
            row.update(r.features)
        out.append(row)
    if not out:
        return pd.DataFrame()
    return pd.DataFrame(out).set_index("timestamp").sort_index()


async def _amain(args: argparse.Namespace) -> int:
    load_dotenv()
    cfg = _load_yaml(args.config)
    pipe = _load_yaml("config/data_pipeline.yaml")

    if args.symbols:
        symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    else:
        symbols = [str(s).strip() for s in (pipe.get("symbols") or []) if str(s).strip()]
    if not symbols:
        print("No symbols. Pass --symbols or configure config/data_pipeline.yaml::symbols")
        return 2

    bt = cfg.get("backtest", {}) or {}
    starting_cash = Decimal(str(bt.get("starting_cash", 100000)))
    fee_bps = Decimal(str(bt.get("fee_bps", 10)))
    slippage_bps = Decimal(str(bt.get("slippage_bps", 5)))
    max_hold_bars = int(bt.get("max_hold_bars", 20))
    train_bars = int(bt.get("walk_forward_train_bars", 252))
    test_bars = int(bt.get("walk_forward_test_bars", 63))
    step_bars = int(bt.get("walk_forward_step_bars", 63))

    thresholds = EdgeGateThresholds.from_yaml(cfg.get("edge_gate"))
    strat_cfg = cfg.get("strategies", {}) or {}
    se_cfg = dict(cfg.get("signal_engine", {}) or {})
    # The edge gate measures a strategy's OWN a-priori, out-of-sample
    # post-cost expectancy in ISOLATION. The trained meta-labeler is a
    # *posterior*, live-fill-trained overlay (the orchestrator's live-P&L
    # trust is the posterior; this verdict is the a-priori prior). Running
    # the gate's backtest THROUGH it is circular and self-fulfilling: a
    # strategy the meta-labeler has soured on (e.g. momentum_breakout, whose
    # buys it scores ~0.15 < 0.228 threshold → 100% dropped) emits zero
    # entries → zero round-trips → permanent ``insufficient_data`` → never
    # proven → never un-soured. Disable it here, consistent with the
    # accumulator already being off (``accumulator=None`` below) and the
    # harness disabling the wall-clock anti-churn gate. The meta-labeler
    # still runs live in production on top of the gate verdict.
    se_cfg["use_trained_meta_labeler"] = False

    # Decide which strategies to evaluate: enabled per-symbol strategies.
    only = {s.strip() for s in (args.only or "").split(",") if s.strip()}
    to_eval: dict[str, Any] = {}
    for name, (cls, cfg_key) in _PER_SYMBOL_STRATEGIES.items():
        if only and name not in only:
            continue
        scfg = dict(strat_cfg.get(cfg_key, {}) or {})
        if only or args.all or scfg.get("enabled", False):
            # The gate tests the strategy's LOGIC, independent of its live
            # deployment flag — force-enable the instance so a strategy that
            # is gated off in config (awaiting proof) can still be evaluated.
            scfg["enabled"] = True
            try:
                to_eval[name] = cls(scfg)
            except Exception as exc:  # noqa: BLE001
                print(f"  ! could not instantiate {name}: {exc}")
    if not to_eval:
        print("No per-symbol strategies enabled. Nothing to evaluate.")
        return 2

    engine, sf = await init_async_database()
    if sf is None:
        print("No DB. Check POSTGRES_* and that Docker is up.")
        return 1

    # Preload features once per symbol (reused across strategies).
    features: dict[str, pd.DataFrame] = {}
    async with sf() as session:
        for sym in symbols:
            q = await session.execute(
                select(FeatureSnapshot)
                .where(FeatureSnapshot.symbol == sym, FeatureSnapshot.timeframe == args.timeframe)
                .order_by(FeatureSnapshot.bar_timestamp.asc())
            )
            df = _rows_to_frame(list(q.scalars().all()))
            if not df.empty:
                features[sym] = df

    if not features:
        print(f"No feature rows for timeframe={args.timeframe}. Run the pipeline first.")
        if engine is not None:
            await engine.dispose()
        return 2

    registry = EdgeGateRegistry(args.out).load()
    print(f"\nEdge gate - timeframe={args.timeframe} symbols_with_data={len(features)} "
          f"fee_bps={fee_bps} slippage_bps={slippage_bps}")
    print("=" * 104)
    print(f"{'strategy':<22}{'verdict':<16}{'mult':>6}{'trades':>9}{'net_pnl':>14}"
          f"{'exp/trade':>12}{'consist':>9}{'pf':>8}{'wins%':>8}")
    print("-" * 104)

    # Adapt walk-forward window sizes to the data actually available. With
    # thin history (e.g. ~37 hourly bars/symbol) the configured 252+63 windows
    # never fit, so nothing gets evaluated. We shrink train/test/step to fit
    # each symbol while keeping a genuine out-of-sample split. This also
    # surfaces the data-sparsity problem honestly in the printed sample size.
    max_bars = max((len(df) for df in features.values()), default=0)
    print(f"  data depth: max {max_bars} bars/symbol "
          f"(configured walk-forward train+test={train_bars + test_bars})")

    def _fit_windows(n: int) -> tuple[int, int, int] | None:
        if n >= train_bars + test_bars:
            return train_bars, test_bars, step_bars
        # Adaptive: 60% train / 25% test, stepping by the test size. Require a
        # minimum so a window has enough bars for lookbacks to warm up.
        if n < args.min_bars:
            return None
        tr = max(20, int(n * 0.60))
        te = max(10, int(n * 0.25))
        if tr + te > n:
            tr = n - te
        if tr < 15 or te < 8:
            return None
        return tr, te, te

    for name, strat in to_eval.items():
        all_windows: list[Any] = []
        syms_used = 0
        # Each per-symbol strategy gets its own SignalEngine (accumulator off
        # for a clean, deterministic backtest of the strategy's own edge).
        se = SignalEngine(se_cfg, accumulator=None)
        for sym, df in features.items():
            win = _fit_windows(len(df))
            if win is None:
                continue
            tr, te, st = win
            try:
                wf = run_walk_forward_backtest(
                    symbol=sym, features=df, strategy=strat, signal_engine=se,
                    starting_cash=starting_cash, fee_bps=fee_bps, slippage_bps=slippage_bps,
                    train_bars=tr, test_bars=te, step_bars=st,
                    max_hold_bars=max_hold_bars,
                )
            except Exception as exc:  # noqa: BLE001
                print(f"  ! {name} on {sym} failed: {exc}")
                continue
            if wf.window_results:
                all_windows.extend(wf.window_results)
                syms_used += 1

        metrics = aggregate_walk_forward(name, all_windows, symbols_evaluated=syms_used)
        verdict = decide_verdict(metrics, thresholds)
        registry.set_verdict(verdict)
        print(f"{name:<22}{verdict.verdict:<16}{str(verdict.size_multiplier):>6}"
              f"{metrics.total_trades:>9}{float(metrics.total_net_pnl):>14.2f}"
              f"{float(metrics.expectancy_per_trade):>12.4f}{float(metrics.consistency):>9.2f}"
              f"{float(metrics.profit_factor):>8.2f}{metrics.avg_win_rate*100:>7.1f}%")

    print("-" * 104)
    if args.dry_run:
        print("DRY RUN - registry NOT written.")
    else:
        registry.save()
        print(f"Wrote {len([v for v in registry.all_verdicts()])} verdicts -> {args.out}")
        print("Enable enforcement with config/strategies.yaml::edge_gate.enabled: true")

    if engine is not None:
        await engine.dispose()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Strategy edge gate - prove post-cost expectancy before capital.")
    ap.add_argument("--config", default="config/strategies.yaml")
    ap.add_argument("--symbols", default="", help="comma-separated; default = data_pipeline.yaml symbols")
    ap.add_argument("--timeframe", default="1d")
    ap.add_argument("--out", default=DEFAULT_REGISTRY_PATH)
    ap.add_argument("--all", action="store_true", help="evaluate all per-symbol strategies even if disabled")
    ap.add_argument("--only", default="", help="comma-separated strategy names to evaluate (subset)")
    ap.add_argument("--min-bars", type=int, default=40,
                    help="minimum bars/symbol to attempt an adaptive walk-forward split")
    ap.add_argument("--dry-run", action="store_true", help="print table; do not write registry")
    return asyncio.run(_amain(ap.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
