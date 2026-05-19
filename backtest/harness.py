"""
M3 backtest harness on feature store snapshots.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

import pandas as pd

from signals.engine import SignalEngine
from strategies.base import Strategy
from backtest.validation import combinatorial_purged_splits


@dataclass
class BacktestResult:
    symbol: str
    trades: int
    wins: int
    losses: int
    final_equity: Decimal
    net_pnl: Decimal
    win_rate: float


@dataclass
class WalkForwardResult:
    symbol: str
    windows: int
    total_trades: int
    aggregate_net_pnl: Decimal
    average_win_rate: float
    window_results: list[BacktestResult]


@dataclass
class PurgedCvResult:
    symbol: str
    folds: int
    fold_results: list[BacktestResult]


def run_backtest_on_features(
    *,
    symbol: str,
    features: pd.DataFrame,
    strategy: Strategy,
    signal_engine: SignalEngine,
    starting_cash: Decimal,
    fee_bps: Decimal,
    slippage_bps: Decimal,
    max_hold_bars: int = 20,
) -> BacktestResult:
    if features is None or features.empty:
        return BacktestResult(symbol, 0, 0, 0, starting_cash, Decimal("0"), 0.0)

    # D115 — the anti-churn gate compares against wall-clock time, but a
    # backtest replays many historical bars within milliseconds of real
    # time. Disable the gate for the duration of this run so historical
    # signals are not mistaken for live-time duplicates. Restored after.
    _ac_orig = getattr(signal_engine, "anti_churn", None)
    if _ac_orig is not None:
        signal_engine.anti_churn = None

    df = features.sort_index().copy()
    cash = Decimal(starting_cash)
    position_qty = Decimal("0")
    entry_cost = Decimal("0")
    bars_held = 0
    trades = 0
    wins = 0
    losses = 0

    fee_mult = fee_bps / Decimal("10000")
    slip_mult = slippage_bps / Decimal("10000")

    for i in range(1, len(df)):
        window = df.iloc[: i + 1]
        latest = window.iloc[-1]
        price = Decimal(str(latest["close"]))
        raw = strategy.generate_signal(symbol, window)
        signal = (
            signal_engine.process(raw, portfolio_value=cash + position_qty * price)
            if raw is not None
            else None
        )

        if position_qty > 0:
            bars_held += 1

        should_exit = (
            position_qty > 0
            and (
                (signal is not None and signal.side == "sell")
                or (max_hold_bars > 0 and bars_held >= max_hold_bars)
            )
        )
        if should_exit:
            exec_px = price * (Decimal("1") - slip_mult)
            gross = exec_px * position_qty
            fee = gross * fee_mult
            proceeds = gross - fee
            cash += proceeds
            pnl = proceeds - entry_cost
            trades += 1
            if pnl >= 0:
                wins += 1
            else:
                losses += 1
            position_qty = Decimal("0")
            entry_cost = Decimal("0")
            bars_held = 0

        if position_qty == 0 and signal is not None and signal.side == "buy":
            qty = signal.suggested_quantity
            if qty <= 0:
                continue
            exec_px = price * (Decimal("1") + slip_mult)
            gross = exec_px * qty
            fee = gross * fee_mult
            total_cost = gross + fee
            if total_cost <= cash:
                cash -= total_cost
                position_qty = qty
                entry_cost = total_cost
                bars_held = 0

    if position_qty > 0:
        final_px = Decimal(str(df.iloc[-1]["close"]))
        gross = final_px * position_qty
        fee = gross * fee_mult
        proceeds = gross - fee
        cash += proceeds
        pnl = proceeds - entry_cost
        trades += 1
        if pnl >= 0:
            wins += 1
        else:
            losses += 1

    # Restore the anti-churn gate for the caller's engine.
    if _ac_orig is not None:
        signal_engine.anti_churn = _ac_orig

    net = cash - Decimal(starting_cash)
    wr = (wins / trades) if trades else 0.0
    return BacktestResult(
        symbol=symbol,
        trades=trades,
        wins=wins,
        losses=losses,
        final_equity=cash,
        net_pnl=net,
        win_rate=wr,
    )


def run_walk_forward_backtest(
    *,
    symbol: str,
    features: pd.DataFrame,
    strategy: Strategy,
    signal_engine: SignalEngine,
    starting_cash: Decimal,
    fee_bps: Decimal,
    slippage_bps: Decimal,
    train_bars: int,
    test_bars: int,
    step_bars: int,
    max_hold_bars: int = 20,
) -> WalkForwardResult:
    """
    Walk-forward validation by evaluating repeated rolling test windows.
    Strategy state is stateless here, so train window is used to mimic realistic split logic.
    """
    if features is None or features.empty:
        return WalkForwardResult(symbol, 0, 0, Decimal("0"), 0.0, [])
    if train_bars <= 0 or test_bars <= 0 or step_bars <= 0:
        raise ValueError("train_bars, test_bars, and step_bars must be > 0")

    df = features.sort_index().copy()
    results: list[BacktestResult] = []
    start = 0
    n = len(df)
    while True:
        train_end = start + train_bars
        test_end = train_end + test_bars
        if test_end > n:
            break
        test_slice = df.iloc[train_end:test_end]
        if not test_slice.empty:
            res = run_backtest_on_features(
                symbol=symbol,
                features=test_slice,
                strategy=strategy,
                signal_engine=signal_engine,
                starting_cash=starting_cash,
                fee_bps=fee_bps,
                slippage_bps=slippage_bps,
                max_hold_bars=max_hold_bars,
            )
            results.append(res)
        start += step_bars

    if not results:
        return WalkForwardResult(symbol, 0, 0, Decimal("0"), 0.0, [])

    total_trades = sum(r.trades for r in results)
    aggregate_net_pnl = sum((r.net_pnl for r in results), Decimal("0"))
    avg_wr = sum(r.win_rate for r in results) / len(results)
    return WalkForwardResult(
        symbol=symbol,
        windows=len(results),
        total_trades=total_trades,
        aggregate_net_pnl=aggregate_net_pnl,
        average_win_rate=avg_wr,
        window_results=results,
    )


def run_purged_cv_backtest(
    *,
    symbol: str,
    features: pd.DataFrame,
    strategy: Strategy,
    signal_engine: SignalEngine,
    starting_cash: Decimal,
    fee_bps: Decimal,
    slippage_bps: Decimal,
    n_splits: int,
    n_test_splits: int,
    embargo_bars: int,
    max_hold_bars: int = 20,
) -> PurgedCvResult:
    if features is None or features.empty:
        return PurgedCvResult(symbol=symbol, folds=0, fold_results=[])
    df = features.sort_index().copy()
    splits = combinatorial_purged_splits(
        len(df),
        n_splits=n_splits,
        n_test_splits=n_test_splits,
        embargo_bars=embargo_bars,
    )
    out: list[BacktestResult] = []
    for _train_idx, test_idx in splits:
        if not test_idx:
            continue
        test_slice = df.iloc[test_idx]
        if test_slice.empty:
            continue
        out.append(
            run_backtest_on_features(
                symbol=symbol,
                features=test_slice,
                strategy=strategy,
                signal_engine=signal_engine,
                starting_cash=starting_cash,
                fee_bps=fee_bps,
                slippage_bps=slippage_bps,
                max_hold_bars=max_hold_bars,
            )
        )
    return PurgedCvResult(symbol=symbol, folds=len(out), fold_results=out)

