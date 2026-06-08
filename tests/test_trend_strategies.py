"""
tests/test_trend_strategies.py
==============================

Unit coverage for the D158 trend weapons (trend_breakout sniper,
trend_following shotgun): they must fire WITH the move (buy breakouts/uptrends,
sell breakdowns/downtrends), stay quiet in chop, respect the enabled flag, and
emit a sizing ``target_notional`` so the signal engine can size them.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from strategies.trend_breakout import TrendBreakoutStrategy
from strategies.trend_following import TrendFollowingStrategy


def _frame(prices: np.ndarray, hi_pad: float = 0.2, lo_pad: float = 0.2) -> pd.DataFrame:
    return pd.DataFrame({
        "open": prices, "high": prices + hi_pad, "low": prices - lo_pad,
        "close": prices, "volume": [1e6] * len(prices),
    })


def _range_then(jump: list[float], seed: int = 0) -> pd.DataFrame:
    base = 100 + np.random.RandomState(seed).randn(60) * 0.5
    return _frame(np.concatenate([base, np.array(jump)]))


# ── trend_breakout (sniper) ─────────────────────────────────────────────────
def test_breakout_fires_long_on_upside_break():
    s = TrendBreakoutStrategy({"enabled": True, "entry_lookback": 50, "atr_lookback": 20,
                               "min_breakout_atr": 0.5, "base_target_notional": "20000"})
    sig = s.generate_signal("X", _range_then([102, 104, 106, 108]))
    assert sig is not None and sig.side == "buy"
    assert sig.metadata["weapon_class"] == "sniper"
    assert float(sig.metadata["target_notional"]) > 0


def test_breakout_fires_short_on_downside_break():
    s = TrendBreakoutStrategy({"enabled": True, "entry_lookback": 50, "atr_lookback": 20,
                               "min_breakout_atr": 0.5, "base_target_notional": "20000"})
    sig = s.generate_signal("X", _range_then([98, 96, 94, 92]))
    assert sig is not None and sig.side == "sell"


def test_breakout_quiet_in_range():
    s = TrendBreakoutStrategy({"enabled": True, "entry_lookback": 50, "atr_lookback": 20,
                               "min_breakout_atr": 0.5, "base_target_notional": "20000"})
    flat = _frame(100 + np.random.RandomState(3).randn(80) * 0.3)
    assert s.generate_signal("X", flat) is None


def test_breakout_respects_enabled_flag():
    s = TrendBreakoutStrategy({"enabled": False, "entry_lookback": 50, "atr_lookback": 20,
                               "base_target_notional": "20000"})
    assert s.generate_signal("X", _range_then([102, 104, 106, 108])) is None


def test_breakout_needs_enough_history():
    s = TrendBreakoutStrategy({"enabled": True, "entry_lookback": 50, "base_target_notional": "20000"})
    assert s.generate_signal("X", _frame(np.linspace(100, 110, 10))) is None


# ── trend_following (shotgun) ───────────────────────────────────────────────
def test_trend_following_buys_uptrend():
    s = TrendFollowingStrategy({"enabled": True, "fast_period": 20, "slow_period": 50,
                                "base_target_notional": "20000"})
    up = _frame(np.linspace(100, 140, 120) + np.random.RandomState(0).randn(120) * 0.5)
    sig = s.generate_signal("X", up)
    assert sig is not None and sig.side == "buy"
    assert sig.metadata["weapon_class"] == "shotgun"


def test_trend_following_sells_downtrend():
    s = TrendFollowingStrategy({"enabled": True, "fast_period": 20, "slow_period": 50,
                                "base_target_notional": "20000"})
    dn = _frame(np.linspace(140, 100, 120) + np.random.RandomState(1).randn(120) * 0.5)
    sig = s.generate_signal("X", dn)
    assert sig is not None and sig.side == "sell"


def test_trend_following_quiet_in_chop():
    s = TrendFollowingStrategy({"enabled": True, "fast_period": 20, "slow_period": 50,
                                "base_target_notional": "20000"})
    flat = _frame(100 + np.random.RandomState(2).randn(120) * 0.3)
    assert s.generate_signal("X", flat) is None


def test_trend_following_rejects_bad_periods():
    s = TrendFollowingStrategy({"enabled": True, "fast_period": 50, "slow_period": 20,
                                "base_target_notional": "20000"})
    up = _frame(np.linspace(100, 140, 120))
    assert s.generate_signal("X", up) is None  # fast >= slow → idle
