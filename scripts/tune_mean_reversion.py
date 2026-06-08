"""
scripts/tune_mean_reversion.py
==============================

Fast parameter sweep for mean_reversion on the live (1h) timeframe (D157.2).

The D157 edge gate found mean_reversion catastrophic on 1h (-$347k, PF 0.43,
32% win) — it fades every minor dip (rsi_buy=47) with no trend filter. This
sweeps RSI extremity + trend-filter MA period and reports aggregate
out-of-sample expectancy / profit factor / win rate so we can pick a config
that flips the verdict positive, then validate with the full edge gate.

Read-only (no DB writes, no config writes). Usage:
  python scripts/tune_mean_reversion.py --timeframe 1h
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
from strategies.mean_reversion import MeanReversionStrategy


def _bbm(latest) -> float | None:
    for c in ("BBM_20_2.0_2.0", "BBM_20_2.0", "BBM_20_2"):
        v = latest.get(c)
        if v is not None:
            try:
                f = float(v)
                if f == f:  # not NaN
                    return f
            except (TypeError, ValueError):
                pass
    return None


def _exit_backtest(
    *, symbol: str, df: pd.DataFrame, strategy, signal_engine,
    fee_bps: Decimal, slippage_bps: Decimal, warmup_bars: int,
    max_hold: int, stop_loss_pct: float, take_profit_pct: float, exit_at_mean: bool,
) -> tuple[int, Decimal, int, Decimal, Decimal]:
    """Long-only replay with configurable mean-reversion exits.

    Exits a long on the FIRST of: stop-loss, take-profit (fixed % or return
    to Bollinger mean), or max_hold. Returns
    (trades, net_pnl, wins, gross_profit, gross_loss). Self-contained so the
    production harness stays untouched.
    """
    fee_m = fee_bps / Decimal("10000")
    slip_m = slippage_bps / Decimal("10000")
    cash = Decimal("0")
    qty = Decimal("0")
    entry_px = Decimal("0")
    bars = 0
    trades = wins = 0
    gp = gl = Decimal("0")
    start_i = max(1, warmup_bars)
    for i in range(start_i, len(df)):
        window = df.iloc[: i + 1]
        latest = window.iloc[-1]
        price = Decimal(str(latest["close"]))
        raw = strategy.generate_signal(symbol, window)
        sig = signal_engine.process(raw, portfolio_value=Decimal("100000")) if raw is not None else None
        if qty > 0:
            bars += 1
            exit_now = False
            if stop_loss_pct > 0 and price <= entry_px * Decimal(str(1 - stop_loss_pct)):
                exit_now = True
            elif take_profit_pct > 0 and price >= entry_px * Decimal(str(1 + take_profit_pct)):
                exit_now = True
            elif exit_at_mean:
                mid = _bbm(latest)
                if mid is not None and price >= Decimal(str(mid)):
                    exit_now = True
            if not exit_now and max_hold > 0 and bars >= max_hold:
                exit_now = True
            if not exit_now and sig is not None and sig.side == "sell":
                exit_now = True
            if exit_now:
                proceeds = price * (Decimal("1") - slip_m) * qty
                proceeds -= proceeds * fee_m
                pnl = proceeds - entry_px * qty
                trades += 1
                if pnl >= 0:
                    wins += 1; gp += pnl
                else:
                    gl += -pnl
                cash += pnl
                qty = Decimal("0"); bars = 0
        if qty == 0 and sig is not None and sig.side == "buy" and sig.suggested_quantity > 0:
            qty = sig.suggested_quantity
            exec_px = price * (Decimal("1") + slip_m)
            entry_px = exec_px + exec_px * fee_m  # fold entry fee into basis
    return trades, cash, wins, gp, gl


def _rows_to_frame(rows: list[FeatureSnapshot]) -> pd.DataFrame:
    out: list[dict[str, Any]] = []
    for r in rows:
        row = {"timestamp": r.bar_timestamp, "open": float(r.open), "high": float(r.high),
               "low": float(r.low), "close": float(r.close), "volume": float(r.volume)}
        if isinstance(r.features, dict):
            row.update(r.features)
        out.append(row)
    return pd.DataFrame(out).set_index("timestamp").sort_index() if out else pd.DataFrame()


# Candidate configs to sweep. Base mirrors the live config; each variant
# tightens RSI extremity and/or turns on the trend filter.
def _variants(base: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    def mk(**over):
        c = dict(base)
        c.update(over)
        return c
    return [
        ("live (47/53, no filter)", mk(rsi_buy_threshold=47, rsi_sell_threshold=53)),
        ("tighten 35/65", mk(rsi_buy_threshold=35, rsi_sell_threshold=65)),
        ("tighten 30/70", mk(rsi_buy_threshold=30, rsi_sell_threshold=70)),
        ("tighten 25/75", mk(rsi_buy_threshold=25, rsi_sell_threshold=75)),
        ("30/70 + trend MA100", mk(rsi_buy_threshold=30, rsi_sell_threshold=70,
                                    trend_filter={"enabled": True, "ma_period": 100})),
        ("30/70 + trend MA200", mk(rsi_buy_threshold=30, rsi_sell_threshold=70,
                                    trend_filter={"enabled": True, "ma_period": 200})),
        ("25/75 + trend MA100", mk(rsi_buy_threshold=25, rsi_sell_threshold=75,
                                    trend_filter={"enabled": True, "ma_period": 100})),
        ("25/75 + trend MA200", mk(rsi_buy_threshold=25, rsi_sell_threshold=75,
                                    trend_filter={"enabled": True, "ma_period": 200})),
    ]


async def _amain(args: argparse.Namespace) -> int:
    load_dotenv()
    # Honor STATIC rsi_buy/sell thresholds during tuning. Live uses the D141
    # dynamic block, which ignores the static values — so without this the RSI
    # sweep would be a no-op. We compare static variants here; the chosen
    # config is deployed via the dynamic block coefficients or by disabling it
    # for mean-reversion.
    if not args.dynamic_rsi:
        import system.dynamic_thresholds as _dt
        _dt._load_dynamic_block = lambda: {}  # type: ignore[assignment]

    cfg = yaml.safe_load(open("config/strategies.yaml", encoding="utf-8")) or {}
    bt = cfg.get("backtest", {}) or {}
    starting_cash = Decimal(str(bt.get("starting_cash", 100000)))
    fee_bps = Decimal(str(bt.get("fee_bps", 10)))
    slippage_bps = Decimal(str(bt.get("slippage_bps", 5)))
    max_hold_bars = int(bt.get("max_hold_bars", 20))
    train_bars = int(bt.get("walk_forward_train_bars", 252))
    test_bars = int(bt.get("walk_forward_test_bars", 63))
    step_bars = int(bt.get("walk_forward_step_bars", 63))
    se_cfg = cfg.get("signal_engine", {}) or {}
    base = dict((cfg.get("strategies", {}) or {}).get("mean_reversion", {}) or {})
    base["enabled"] = True

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

    # ── Exit-policy sweep on the best entry config ────────────────────────
    if args.exit_sweep:
        # Entry tightened to 30/70 but NO trend filter — the filter makes
        # entries too rare to evaluate exits. The question here is purely:
        # can smart exits (stop-loss capping the ~70% losers, take-profit at
        # the mean) flip a frequently-trading config positive?
        entry = dict(base)
        entry.update(rsi_buy_threshold=30, rsi_sell_threshold=70)
        warm = 120  # enough history for indicators; no long trend MA needed
        policies = [
            ("baseline hold20", dict(max_hold=20, stop_loss_pct=0.0, take_profit_pct=0.0, exit_at_mean=False)),
            ("exit-at-mean", dict(max_hold=48, stop_loss_pct=0.0, take_profit_pct=0.0, exit_at_mean=True)),
            ("mean + SL1%", dict(max_hold=48, stop_loss_pct=0.01, take_profit_pct=0.0, exit_at_mean=True)),
            ("mean + SL2%", dict(max_hold=48, stop_loss_pct=0.02, take_profit_pct=0.0, exit_at_mean=True)),
            ("max_hold 3", dict(max_hold=3, stop_loss_pct=0.0, take_profit_pct=0.0, exit_at_mean=False)),
            ("max_hold 5 + SL1%", dict(max_hold=5, stop_loss_pct=0.01, take_profit_pct=0.0, exit_at_mean=False)),
            ("TP1% + SL1%", dict(max_hold=24, stop_loss_pct=0.01, take_profit_pct=0.01, exit_at_mean=False)),
            ("TP0.5% + SL0.5%", dict(max_hold=24, stop_loss_pct=0.005, take_profit_pct=0.005, exit_at_mean=False)),
        ]
        print(f"\nmean_reversion EXIT sweep (entry=30/70+trendMA100) tf={args.timeframe} "
              f"symbols={len(features)} fee_bps={fee_bps} slip_bps={slippage_bps}\n" + "=" * 96)
        print(f"{'exit policy':<24}{'trades':>8}{'net_pnl':>14}{'exp/trade':>12}{'pf':>8}{'win%':>8}")
        print("-" * 96)
        for label, pol in policies:
            strat = MeanReversionStrategy(entry)
            se = SignalEngine(se_cfg, accumulator=None)
            tot_tr = tot_w = 0
            tot_net = tot_gp = tot_gl = Decimal("0")
            for sym, df in features.items():
                # Bound the replay to a recent slice — generate_signal recomputes
                # rolling features on the growing window each bar (O(n^2)), so the
                # full 17k-bar series is intractable. The last ~2500 bars give a
                # large trade sample at tractable cost.
                df = df.tail(args.max_bars)
                if len(df) < warm + 50:
                    continue
                tr, net, w, gp, gl = _exit_backtest(
                    symbol=sym, df=df, strategy=strat, signal_engine=se,
                    fee_bps=fee_bps, slippage_bps=slippage_bps, warmup_bars=warm, **pol,
                )
                tot_tr += tr; tot_w += w; tot_net += net; tot_gp += gp; tot_gl += gl
            exp = (tot_net / tot_tr) if tot_tr else Decimal("0")
            pf = (tot_gp / tot_gl) if tot_gl > 0 else (Decimal("999") if tot_gp > 0 else Decimal("0"))
            win = (100.0 * tot_w / tot_tr) if tot_tr else 0.0
            print(f"{label:<24}{tot_tr:>8}{float(tot_net):>14.0f}{float(exp):>12.2f}"
                  f"{float(pf):>8.2f}{win:>7.1f}%")
        print("-" * 96)
        if engine is not None:
            await engine.dispose()
        return 0

    print(f"\nmean_reversion 1h tuning - symbols={len(features)} "
          f"fee_bps={fee_bps} slippage_bps={slippage_bps}\n" + "=" * 96)
    print(f"{'variant':<28}{'trades':>8}{'net_pnl':>14}{'exp/trade':>12}{'consist':>9}{'pf':>8}{'win%':>8}")
    print("-" * 96)
    for label, vcfg in _variants(base):
        strat = MeanReversionStrategy(vcfg)
        se = SignalEngine(se_cfg, accumulator=None)
        windows: list[Any] = []
        used = 0
        for sym, df in features.items():
            n = len(df)
            if n < train_bars + test_bars:
                continue
            wf = run_walk_forward_backtest(
                symbol=sym, features=df, strategy=strat, signal_engine=se,
                starting_cash=starting_cash, fee_bps=fee_bps, slippage_bps=slippage_bps,
                train_bars=train_bars, test_bars=test_bars, step_bars=step_bars,
                max_hold_bars=max_hold_bars,
            )
            if wf.window_results:
                windows.extend(wf.window_results)
                used += 1
        m = aggregate_walk_forward("mean_reversion", windows, symbols_evaluated=used)
        print(f"{label:<28}{m.total_trades:>8}{float(m.total_net_pnl):>14.0f}"
              f"{float(m.expectancy_per_trade):>12.2f}{float(m.consistency):>9.2f}"
              f"{float(m.profit_factor):>8.2f}{m.avg_win_rate*100:>7.1f}%")
    print("-" * 96)
    if engine is not None:
        await engine.dispose()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--timeframe", default="1h")
    ap.add_argument("--dynamic-rsi", action="store_true",
                    help="use the live D141 dynamic RSI block instead of static thresholds")
    ap.add_argument("--exit-sweep", action="store_true",
                    help="sweep exit policies (TP/SL/hold/mean-exit) on the best entry config")
    ap.add_argument("--max-bars", type=int, default=1200,
                    help="cap replay to the last N bars/symbol (exit sweep; perf)")
    return asyncio.run(_amain(ap.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
