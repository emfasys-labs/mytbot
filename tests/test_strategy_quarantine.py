from __future__ import annotations

from decimal import Decimal

from system.strategy_quarantine import decide_strategy_quarantine


_CFG = {
    "enabled": True,
    "min_fills": 20,
    "reduced_pnl_per_fill": "-5",
    "blocked_pnl_per_fill": "-15",
    "reduce_only_pnl_per_fill": "-30",
    "reduced_win_rate": "0.40",
    "blocked_win_rate": "0.30",
    "reduce_only_win_rate": "0.20",
    "multipliers": {
        "normal": "1.00",
        "reduced_size": "0.50",
        "blocked_new_opens": "0.00",
        "reduce_only": "0.00",
    },
}


def test_quarantine_is_neutral_when_config_incomplete() -> None:
    d = decide_strategy_quarantine("volatility_regime", {"fills": 100}, {"enabled": True})
    assert d.state == "normal"
    assert d.multiplier == Decimal("1")


def test_quarantine_waits_for_minimum_sample() -> None:
    d = decide_strategy_quarantine(
        "volatility_regime",
        {"fills": 3, "net_pnl": "-1000", "win_rate": 0.0},
        _CFG,
    )
    assert d.state == "learning"
    assert d.multiplier == Decimal("1.00")


def test_quarantine_reduces_losing_strategy_size() -> None:
    d = decide_strategy_quarantine(
        "volume_flow",
        {"fills": 40, "net_pnl": "-240", "win_rate": "0.45"},
        _CFG,
    )
    assert d.state == "reduced_size"
    assert d.multiplier == Decimal("0.50")


def test_quarantine_blocks_bad_expectancy_opens() -> None:
    d = decide_strategy_quarantine(
        "volatility_regime",
        {"fills": 40, "net_pnl": "-800", "win_rate": "0.28"},
        _CFG,
    )
    assert d.state == "blocked_new_opens"
    assert d.multiplier == Decimal("0.00")


def test_quarantine_reduce_only_for_deeply_bad_strategy() -> None:
    d = decide_strategy_quarantine(
        "volatility_regime",
        {"fills": 40, "net_pnl": "-1400", "win_rate": "0.18"},
        _CFG,
    )
    assert d.state == "reduce_only"
    assert d.multiplier == Decimal("0.00")
