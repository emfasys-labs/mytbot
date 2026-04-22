import pandas as pd
from decimal import Decimal

from strategies.mean_reversion import MeanReversionStrategy
from strategies.momentum import MomentumBreakoutStrategy


def _base_df(n: int = 80) -> pd.DataFrame:
    idx = pd.date_range("2024-01-01", periods=n, freq="D", tz="UTC")
    return pd.DataFrame(
        {
            "open": [100 + i * 0.1 for i in range(n)],
            "high": [101 + i * 0.1 for i in range(n)],
            "low": [99 + i * 0.1 for i in range(n)],
            "close": [100 + i * 0.1 for i in range(n)],
            "volume": [1_000_000.0] * n,
            "rsi_14": [50.0] * n,
            "BBL_20_2.0": [95.0] * n,
            "BBU_20_2.0": [105.0] * n,
        },
        index=idx,
    )


def test_momentum_breakout_generates_buy_on_breakout():
    df = _base_df()
    # Force breakout on last bar.
    df.iloc[-1, df.columns.get_loc("close")] = 120.0
    df.iloc[-1, df.columns.get_loc("high")] = 121.0
    df.iloc[-1, df.columns.get_loc("volume")] = 5_000_000.0
    strat = MomentumBreakoutStrategy(
        {
            "enabled": True,
            "lookback_periods": 20,
            "volume_multiplier": 1.2,
            "atr_min": 0.0,
            "atr_max": 1.0,
            "momentum_threshold": 0.001,
        }
    )
    sig = strat.generate_signal("SPY", df)
    assert sig is not None
    assert sig.side == "buy"
    assert Decimal(str(sig.metadata["target_notional"])) > 0
    assert sig.metadata["sizing_intent_source"] == "strategy_confidence_volatility"


def test_mean_reversion_generates_buy_when_oversold():
    df = _base_df()
    df.iloc[-1, df.columns.get_loc("close")] = 90.0
    df.iloc[-1, df.columns.get_loc("rsi_14")] = 20.0
    strat = MeanReversionStrategy(
        {
            "enabled": True,
            "lookback_periods": 20,
            "rsi_buy_threshold": 30.0,
            "rsi_sell_threshold": 70.0,
            "band_epsilon": 0.01,
        }
    )
    sig = strat.generate_signal("SPY", df)
    assert sig is not None
    assert sig.side == "buy"
    assert Decimal(str(sig.metadata["target_notional"])) > 0
    assert sig.metadata["sizing_intent_source"] == "strategy_confidence_volatility"


def test_momentum_target_notional_scales_with_atr() -> None:
    df = _base_df()
    df.iloc[-1, df.columns.get_loc("close")] = 120.0
    df.iloc[-1, df.columns.get_loc("high")] = 121.0
    df.iloc[-1, df.columns.get_loc("volume")] = 5_000_000.0

    strat = MomentumBreakoutStrategy(
        {
            "enabled": True,
            "lookback_periods": 20,
            "volume_multiplier": 1.2,
            "atr_min": 0.0,
            "atr_max": 1.0,
            "momentum_threshold": 0.001,
            "base_target_notional": "5000",
        }
    )

    low_atr = df.copy()
    # Very low ATR% => vol scale near upper clamp.
    low_atr.iloc[-1, low_atr.columns.get_loc("low")] = 119.8

    high_atr = df.copy()
    # Wider bar range => higher ATR% => lower notional.
    high_atr.iloc[-1, high_atr.columns.get_loc("low")] = 108.0

    sig_low = strat.generate_signal("SPY", low_atr)
    sig_high = strat.generate_signal("SPY", high_atr)
    assert sig_low is not None and sig_high is not None

    n_low = Decimal(str(sig_low.metadata["target_notional"]))
    n_high = Decimal(str(sig_high.metadata["target_notional"]))
    assert n_low >= n_high

