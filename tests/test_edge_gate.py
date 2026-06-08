"""
tests/test_edge_gate.py
=======================

Locks in the D157 strategy edge gate: a strategy only gets capital after
proving positive post-cost expectancy in out-of-sample backtest.

Covers: verdict decision logic, walk-forward aggregation, the atomic JSON
registry, and the unproven-policy (reduce vs block) cold-start behaviour.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from backtest.edge_gate import (
    EdgeGateRegistry,
    EdgeGateThresholds,
    EdgeVerdict,
    StrategyEdgeMetrics,
    VERDICT_ALLOWED,
    VERDICT_BLOCKED,
    VERDICT_INSUFFICIENT,
    VERDICT_REDUCED,
    aggregate_walk_forward,
    decide_verdict,
)


@dataclass
class _Win:
    trades: int
    net_pnl: Decimal
    win_rate: float = 0.5


def _thr(**over) -> EdgeGateThresholds:
    base = dict(
        enabled=True, min_trades=30,
        block_consistency=Decimal("0.45"), allow_consistency=Decimal("0.55"),
        allow_profit_factor=Decimal("1.10"), reduced_multiplier=Decimal("0.50"),
        unproven_policy="reduce",
    )
    base.update(over)
    return EdgeGateThresholds(**base)


# ── verdict logic ────────────────────────────────────────────────────────────
def test_proven_edge_is_allowed():
    m = StrategyEdgeMetrics(
        strategy="momentum_breakout", symbols_evaluated=20, windows=40,
        profitable_windows=28, total_trades=200, total_net_pnl=Decimal("15000"),
        gross_profit=Decimal("30000"), gross_loss=Decimal("15000"),
    )
    v = decide_verdict(m, _thr())
    assert v.verdict == VERDICT_ALLOWED
    assert v.size_multiplier == Decimal("1")
    assert v.allow_new_capital is True


def test_negative_expectancy_is_blocked():
    m = StrategyEdgeMetrics(
        strategy="mean_reversion", symbols_evaluated=20, windows=40,
        profitable_windows=10, total_trades=200, total_net_pnl=Decimal("-8000"),
        gross_profit=Decimal("5000"), gross_loss=Decimal("13000"),
    )
    v = decide_verdict(m, _thr())
    assert v.verdict == VERDICT_BLOCKED
    assert v.size_multiplier == Decimal("0")
    assert v.allow_new_capital is False


def test_low_consistency_is_blocked_even_if_net_positive():
    # Net positive but only 40% of windows profitable (< block_consistency 0.45).
    m = StrategyEdgeMetrics(
        strategy="volume_flow", symbols_evaluated=10, windows=50,
        profitable_windows=20, total_trades=120, total_net_pnl=Decimal("500"),
        gross_profit=Decimal("12000"), gross_loss=Decimal("11500"),
    )
    v = decide_verdict(m, _thr())
    assert v.verdict == VERDICT_BLOCKED


def test_weak_positive_is_reduced():
    # Positive expectancy + consistency 0.50 (>= block 0.45 but < allow 0.55).
    m = StrategyEdgeMetrics(
        strategy="event_driven_news", symbols_evaluated=10, windows=40,
        profitable_windows=20, total_trades=100, total_net_pnl=Decimal("2000"),
        gross_profit=Decimal("9000"), gross_loss=Decimal("7000"),
    )
    v = decide_verdict(m, _thr())
    assert v.verdict == VERDICT_REDUCED
    assert v.size_multiplier == Decimal("0.50")
    assert v.allow_new_capital is True


def test_insufficient_data_reduce_policy():
    m = StrategyEdgeMetrics(strategy="pairs_trading", windows=2,
                            profitable_windows=2, total_trades=5,
                            total_net_pnl=Decimal("100"), gross_profit=Decimal("100"))
    v = decide_verdict(m, _thr(unproven_policy="reduce"))
    assert v.verdict == VERDICT_INSUFFICIENT
    assert v.allow_new_capital is True
    assert v.size_multiplier == Decimal("0.50")


def test_insufficient_data_block_policy():
    m = StrategyEdgeMetrics(strategy="pairs_trading", windows=2,
                            profitable_windows=2, total_trades=5,
                            total_net_pnl=Decimal("100"), gross_profit=Decimal("100"))
    v = decide_verdict(m, _thr(unproven_policy="block"))
    assert v.verdict == VERDICT_INSUFFICIENT
    assert v.allow_new_capital is False
    assert v.size_multiplier == Decimal("0")


# ── aggregation ──────────────────────────────────────────────────────────────
def test_aggregate_walk_forward_skips_zero_trade_windows():
    wins = [
        _Win(trades=0, net_pnl=Decimal("0")),       # ignored
        _Win(trades=10, net_pnl=Decimal("500"), win_rate=0.6),
        _Win(trades=5, net_pnl=Decimal("-200"), win_rate=0.4),
    ]
    m = aggregate_walk_forward("s", wins, symbols_evaluated=3)
    assert m.windows == 2
    assert m.total_trades == 15
    assert m.total_net_pnl == Decimal("300")
    assert m.profitable_windows == 1
    assert m.gross_profit == Decimal("500")
    assert m.gross_loss == Decimal("200")
    assert m.consistency == Decimal("0.5")
    assert m.profit_factor == Decimal("2.5")


def test_profit_factor_no_losses():
    m = StrategyEdgeMetrics(strategy="s", windows=3, profitable_windows=3,
                            total_trades=30, total_net_pnl=Decimal("900"),
                            gross_profit=Decimal("900"), gross_loss=Decimal("0"))
    assert m.profit_factor == Decimal("999")


# ── registry ─────────────────────────────────────────────────────────────────
def test_registry_roundtrip(tmp_path):
    reg = EdgeGateRegistry(tmp_path / "v.json")
    reg.set_verdict(EdgeVerdict("momentum_breakout", VERDICT_ALLOWED, Decimal("1"), True, "ok"))
    reg.set_verdict(EdgeVerdict("mean_reversion", VERDICT_BLOCKED, Decimal("0"), False, "no edge"))
    reg.save()

    reg2 = EdgeGateRegistry(tmp_path / "v.json").load()
    assert reg2.verdict_for("momentum_breakout").verdict == VERDICT_ALLOWED
    assert reg2.size_multiplier_for("momentum_breakout", _thr()) == Decimal("1")
    assert reg2.is_blocked("mean_reversion", _thr()) is True
    assert reg2.is_blocked("momentum_breakout", _thr()) is False


def test_registry_missing_file_is_empty():
    reg = EdgeGateRegistry("/nonexistent/path/v.json").load()
    assert reg.all_verdicts() == {}


def test_unknown_strategy_follows_unproven_policy():
    reg = EdgeGateRegistry("/nonexistent/v.json").load()
    # reduce policy → unknown strategy gets reduced multiplier, not blocked.
    assert reg.is_blocked("brand_new_strat", _thr(unproven_policy="reduce")) is False
    assert reg.size_multiplier_for("brand_new_strat", _thr(unproven_policy="reduce")) == Decimal("0.50")
    # block policy → unknown strategy blocked.
    assert reg.is_blocked("brand_new_strat", _thr(unproven_policy="block")) is True
    assert reg.size_multiplier_for("brand_new_strat", _thr(unproven_policy="block")) == Decimal("0")


def test_thresholds_from_yaml():
    t = EdgeGateThresholds.from_yaml({
        "enabled": True, "min_trades": 50, "allow_consistency": 0.6,
        "reduced_multiplier": 0.3, "unproven_policy": "block",
    })
    assert t.enabled is True
    assert t.min_trades == 50
    assert t.allow_consistency == Decimal("0.6")
    assert t.reduced_multiplier == Decimal("0.3")
    assert t.unproven_policy == "block"


def test_thresholds_from_yaml_bad_policy_defaults_reduce():
    t = EdgeGateThresholds.from_yaml({"unproven_policy": "garbage"})
    assert t.unproven_policy == "reduce"
