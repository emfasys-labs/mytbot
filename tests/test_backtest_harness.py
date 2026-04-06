from decimal import Decimal

import pandas as pd

from backtest.harness import run_backtest_on_features, run_walk_forward_backtest
from signals.engine import SignalEngine
from strategies.mean_reversion import MeanReversionStrategy


def test_backtest_harness_runs_and_returns_result():
    idx = pd.date_range("2024-01-01", periods=120, freq="D", tz="UTC")
    close = [100 + ((i % 12) - 6) * 0.8 for i in range(120)]
    df = pd.DataFrame(
        {
            "open": close,
            "high": [c + 1 for c in close],
            "low": [c - 1 for c in close],
            "close": close,
            "volume": [1_000_000.0] * 120,
            "rsi_14": [25.0 if i % 12 < 2 else 75.0 if i % 12 > 9 else 50.0 for i in range(120)],
            "BBL_20_2.0": [96.0] * 120,
            "BBU_20_2.0": [104.0] * 120,
        },
        index=idx,
    )
    strategy = MeanReversionStrategy(
        {
            "enabled": True,
            "lookback_periods": 20,
            "rsi_buy_threshold": 30.0,
            "rsi_sell_threshold": 70.0,
            "band_epsilon": 0.02,
        }
    )
    engine = SignalEngine({"default_position_pct": 0.2, "quantity_decimals": 6})
    result = run_backtest_on_features(
        symbol="SPY",
        features=df,
        strategy=strategy,
        signal_engine=engine,
        starting_cash=Decimal("100000"),
        fee_bps=Decimal("10"),
        slippage_bps=Decimal("5"),
        max_hold_bars=10,
    )
    assert result.trades > 0
    assert result.final_equity > 0


def test_walk_forward_backtest_returns_windows():
    idx = pd.date_range("2024-01-01", periods=160, freq="D", tz="UTC")
    close = [100 + ((i % 8) - 4) * 0.6 for i in range(160)]
    df = pd.DataFrame(
        {
            "open": close,
            "high": [c + 1 for c in close],
            "low": [c - 1 for c in close],
            "close": close,
            "volume": [1_000_000.0] * 160,
            "rsi_14": [25.0 if i % 8 < 2 else 75.0 if i % 8 > 5 else 50.0 for i in range(160)],
            "BBL_20_2.0": [97.0] * 160,
            "BBU_20_2.0": [103.0] * 160,
        },
        index=idx,
    )
    strategy = MeanReversionStrategy(
        {
            "enabled": True,
            "lookback_periods": 20,
            "rsi_buy_threshold": 30.0,
            "rsi_sell_threshold": 70.0,
            "band_epsilon": 0.01,
        }
    )
    engine = SignalEngine({"default_position_pct": 0.2, "quantity_decimals": 6})
    wf = run_walk_forward_backtest(
        symbol="SPY",
        features=df,
        strategy=strategy,
        signal_engine=engine,
        starting_cash=Decimal("100000"),
        fee_bps=Decimal("10"),
        slippage_bps=Decimal("5"),
        train_bars=60,
        test_bars=30,
        step_bars=20,
        max_hold_bars=10,
    )
    assert wf.windows > 0
    assert len(wf.window_results) == wf.windows

