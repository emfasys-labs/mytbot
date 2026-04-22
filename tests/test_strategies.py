import pandas as pd
from decimal import Decimal

from strategies.mean_reversion import MeanReversionStrategy
from strategies.momentum import MomentumBreakoutStrategy
from strategies.volume_flow import VolumeFlowStrategy
from strategies.event_driven import EventDrivenNewsStrategy
from strategies.pairs_trading import PairsTradingStrategy
from strategies.volatility_regime import VolatilityRegimeStrategy
from strategies.regime_rotation import RegimeRotationStrategy


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


def test_volume_flow_generates_continuation_signal() -> None:
    df = _base_df()
    df.iloc[-2, df.columns.get_loc("close")] = 100.0
    df.iloc[-1, df.columns.get_loc("close")] = 103.0
    df.iloc[-1, df.columns.get_loc("volume")] = 6_000_000.0
    strat = VolumeFlowStrategy({"enabled": True, "zscore_open_threshold": 1.2, "min_bar_return": 0.001})
    sig = strat.generate_signal("SPY", df)
    assert sig is not None
    assert sig.side in ("buy", "sell")
    assert Decimal(str(sig.metadata["target_notional"])) > 0


def test_event_driven_news_generates_on_shock() -> None:
    strat = EventDrivenNewsStrategy({"enabled": True, "shock_threshold": 0.4})
    sig = strat.generate_from_context(
        symbol="SPY",
        asset_class="equity",
        news_score=0.88,
        news_detail={"topic": "macro"},
        macro_regime="risk_on",
        macro_confidence=0.8,
    )
    assert sig is not None
    assert sig.side == "buy"
    assert Decimal(str(sig.metadata["target_notional"])) > 0


def test_pairs_trading_generates_signal_on_spread_dislocation() -> None:
    idx = pd.date_range("2024-01-01", periods=120, freq="D", tz="UTC")
    spy = pd.DataFrame({"close": [100 + i * 0.2 for i in range(120)]}, index=idx)
    qqq = pd.DataFrame({"close": [95 + i * 0.2 for i in range(120)]}, index=idx)
    # Distort last bar so z-score breaches open threshold.
    spy.iloc[-1, spy.columns.get_loc("close")] += 8.0
    strat = PairsTradingStrategy(
        {"enabled": True, "pairs": [["SPY", "QQQ"]], "lookback_bars": 90, "zscore_open": 1.0}
    )
    out = strat.generate_signals({"SPY": spy, "QQQ": qqq})
    assert len(out) == 1
    assert out[0].strategy == "pairs_trading"


def test_volatility_regime_generates_breakout_signal() -> None:
    df = _base_df(120)
    # Force high TR and directional impulse on final bars.
    df.iloc[-2, df.columns.get_loc("close")] = 100.0
    df.iloc[-1, df.columns.get_loc("close")] = 105.0
    df.iloc[-1, df.columns.get_loc("high")] = 108.0
    df.iloc[-1, df.columns.get_loc("low")] = 96.0
    strat = VolatilityRegimeStrategy(
        {
            "enabled": True,
            "atr_lookback": 10,
            "atr_expansion_ratio": 1.05,
            "min_bar_return": 0.001,
        }
    )
    sig = strat.generate_signal("SPY", df)
    assert sig is not None
    assert sig.strategy == "volatility_regime"


def test_regime_rotation_generates_proxy_signal() -> None:
    strat = RegimeRotationStrategy(
        {
            "enabled": True,
            "score_trigger": 0.2,
            "risk_on_symbols": ["SPY"],
            "risk_off_symbols": ["TLT"],
        }
    )
    sig = strat.generate_from_demand(
        symbol="SPY",
        asset_class="equity",
        demand_score=0.6,
        demand_trend="rising",
        demand_confidence=0.8,
    )
    assert sig is not None
    assert sig.side == "buy"

