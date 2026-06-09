from decimal import Decimal

import pandas as pd

from backtest.harness import run_backtest_on_features, run_walk_forward_backtest
from signals.engine import RawSignal
from signals.engine import SignalEngine
from strategies.mean_reversion import MeanReversionStrategy
from strategies.base import Strategy


class _ScriptedShortStrategy(Strategy):
    name = "scripted_short"

    def generate_signal(self, symbol, features):
        # Carry the bar price so the signal engine sizes a sane quantity
        # (without it, the engine's notional-as-quantity fallback produces a
        # 20x-equity short that the D161 margin guard correctly rejects).
        px = float(features["close"].iloc[-1])
        i = len(features) - 1
        if i == 1:
            return RawSignal(
                strategy=self.name,
                symbol=symbol,
                side="sell",
                confidence=0.9,
                broker="ibkr",
                asset_class="equity",
                metadata={"close": px},
            )
        if i == 4:
            return RawSignal(
                strategy=self.name,
                symbol=symbol,
                side="buy",
                confidence=0.9,
                broker="ibkr",
                asset_class="equity",
                metadata={"close": px},
            )
        return None


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


def test_backtest_harness_can_replay_validated_shorts():
    idx = pd.date_range("2024-01-01", periods=8, freq="D", tz="UTC")
    close = [100, 100, 96, 92, 90, 91, 92, 93]
    df = pd.DataFrame(
        {
            "open": close,
            "high": [c + 1 for c in close],
            "low": [c - 1 for c in close],
            "close": close,
            "volume": [1_000_000.0] * len(close),
        },
        index=idx,
    )
    engine = SignalEngine({"default_position_pct": 0.2, "quantity_decimals": 6})
    result = run_backtest_on_features(
        symbol="SPY",
        features=df,
        strategy=_ScriptedShortStrategy({"enabled": True}),
        signal_engine=engine,
        starting_cash=Decimal("100000"),
        fee_bps=Decimal("0"),
        slippage_bps=Decimal("0"),
        max_hold_bars=10,
        allow_shorts=True,
    )
    # The buy at bar 4 covers the short AND flips long (priced signal, both
    # sides allowed) — the long is force-closed at the end at a small profit.
    assert result.short_trades == 1
    assert result.long_trades == 1
    assert result.trades == 2
    assert result.net_pnl > 0


def _short_df(n: int = 8):
    idx = pd.date_range("2024-01-01", periods=n, freq="D", tz="UTC")
    close = [100, 100, 96, 92, 90, 91, 92, 93][:n]
    return pd.DataFrame(
        {
            "open": close,
            "high": [c + 1 for c in close],
            "low": [c - 1 for c in close],
            "close": close,
            "volume": [1_000_000.0] * len(close),
        },
        index=idx,
    )


class _UnpricedShortStrategy(Strategy):
    """Emits an unpriced sell — the engine's notional-as-quantity fallback
    would produce a 20x-equity short without the D161 margin guard."""

    name = "unpriced_short"

    def generate_signal(self, symbol, features):
        if len(features) - 1 == 1:
            return RawSignal(
                strategy=self.name, symbol=symbol, side="sell",
                confidence=0.9, broker="ibkr", asset_class="equity",
            )
        return None


def test_short_margin_guard_rejects_oversized_short():
    engine = SignalEngine({"default_position_pct": 0.2, "quantity_decimals": 6})
    result = run_backtest_on_features(
        symbol="SPY", features=_short_df(),
        strategy=_UnpricedShortStrategy({"enabled": True}), signal_engine=engine,
        starting_cash=Decimal("100000"), fee_bps=Decimal("0"), slippage_bps=Decimal("0"),
        max_hold_bars=10, allow_shorts=True,
    )
    # gross ($2M) > equity ($100k) → entry refused, equity untouched.
    assert result.trades == 0
    assert result.final_equity == Decimal("100000")


def test_short_only_mode_skips_long_entries():
    engine = SignalEngine({"default_position_pct": 0.2, "quantity_decimals": 6})
    result = run_backtest_on_features(
        symbol="SPY", features=_short_df(),
        strategy=_ScriptedShortStrategy({"enabled": True}), signal_engine=engine,
        starting_cash=Decimal("100000"), fee_bps=Decimal("0"), slippage_bps=Decimal("0"),
        max_hold_bars=10, allow_shorts=True, allow_longs=False,
    )
    # The scripted sell@100/cover-on-buy@90 round-trip still happens (short
    # side), but the buy signal may only COVER — never open a long.
    assert result.short_trades == 1
    assert result.long_trades == 0
    assert result.net_pnl > 0


def test_long_only_mode_unchanged_by_default():
    engine = SignalEngine({"default_position_pct": 0.2, "quantity_decimals": 6})
    result = run_backtest_on_features(
        symbol="SPY", features=_short_df(),
        strategy=_ScriptedShortStrategy({"enabled": True}), signal_engine=engine,
        starting_cash=Decimal("100000"), fee_bps=Decimal("0"), slippage_bps=Decimal("0"),
        max_hold_bars=10,
    )
    # Default (legacy) behaviour: shorts off — the sell signal opens nothing.
    assert result.short_trades == 0
