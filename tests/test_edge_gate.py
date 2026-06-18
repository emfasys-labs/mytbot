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


# ── D161 — per-side verdicts ─────────────────────────────────────────────────
def test_registry_per_side_verdicts(tmp_path):
    reg = EdgeGateRegistry(tmp_path / "v.json")
    reg.set_verdict(EdgeVerdict("mean_reversion", VERDICT_ALLOWED, Decimal("1"), True, "long ok"))
    reg.set_verdict(EdgeVerdict(EdgeGateRegistry.short_key("mean_reversion"),
                                VERDICT_BLOCKED, Decimal("0"), False, "short toxic"))
    reg.save()
    reg2 = EdgeGateRegistry(tmp_path / "v.json").load()
    # Long side allowed, short side blocked — same strategy.
    assert reg2.is_blocked("mean_reversion", _thr(), side="long") is False
    assert reg2.is_blocked("mean_reversion", _thr(), side="short") is True
    assert reg2.is_blocked("mean_reversion", _thr(), side="sell") is True   # alias
    assert reg2.size_multiplier_for("mean_reversion", _thr(), side="short") == Decimal("0")
    assert reg2.size_multiplier_for("mean_reversion", _thr(), side="long") == Decimal("1")


def test_missing_short_verdict_follows_unproven_policy():
    reg = EdgeGateRegistry("/nonexistent/v.json").load()
    reg.set_verdict(EdgeVerdict("trend_breakout", VERDICT_ALLOWED, Decimal("1"), True, "ok"))
    # No #short verdict → unproven policy decides the short side.
    assert reg.is_blocked("trend_breakout", _thr(unproven_policy="reduce"), side="short") is False
    assert reg.size_multiplier_for("trend_breakout", _thr(unproven_policy="reduce"), side="short") == Decimal("0.50")
    assert reg.is_blocked("trend_breakout", _thr(unproven_policy="block"), side="short") is True


def test_default_side_is_long_backward_compat():
    reg = EdgeGateRegistry("/nonexistent/v.json").load()
    reg.set_verdict(EdgeVerdict("momentum_breakout", VERDICT_BLOCKED, Decimal("0"), False, "no"))
    # 2-arg call signature (pre-D161 readers) still resolves the long verdict.
    assert reg.is_blocked("momentum_breakout", _thr()) is True


def test_insufficient_data_with_negative_expectancy_is_blocked():
    # D161 — 25 trades at clearly negative expectancy is adverse evidence,
    # not a cold start: no capital even under the reduce policy.
    m = StrategyEdgeMetrics(strategy="trend_breakout#short", windows=10,
                            profitable_windows=3, total_trades=25,
                            total_net_pnl=Decimal("-26288"),
                            gross_profit=Decimal("4000"), gross_loss=Decimal("30288"))
    v = decide_verdict(m, _thr(unproven_policy="reduce"))
    assert v.verdict == VERDICT_INSUFFICIENT
    assert v.allow_new_capital is False
    assert v.size_multiplier == Decimal("0")


def test_zero_trades_keeps_cold_start_reduce():
    m = StrategyEdgeMetrics(strategy="brand_new", windows=0, total_trades=0)
    v = decide_verdict(m, _thr(unproven_policy="reduce"))
    assert v.allow_new_capital is True
    assert v.size_multiplier == Decimal("0.50")


# ── D163 — edge-proportional Kelly trust ─────────────────────────────────────
def test_kelly_fraction_closed_form():
    from backtest.edge_gate import kelly_fraction

    # f* = W·(PF−1)/PF. trend_breakout-like: W .73, PF 4.0 → .73·.75 = .5475
    assert kelly_fraction(0.73, Decimal("4.0")) == Decimal("0.73") * Decimal("3.0") / Decimal("4.0")
    # Stronger edge beats weaker edge.
    strong = kelly_fraction(0.73, Decimal("4.0"))
    weak = kelly_fraction(0.64, Decimal("1.9"))
    assert strong > weak
    # No edge (PF<=1 or W<=0) → 0; clamped to [0,1].
    assert kelly_fraction(0.6, Decimal("1.0")) == Decimal("0")
    assert kelly_fraction(0.0, Decimal("3.0")) == Decimal("0")
    assert kelly_fraction(0.99, Decimal("999")) <= Decimal("1")


def _verdict_with_stats(name, verdict, allow, win_rate, pf):
    return EdgeVerdict(
        strategy=name, verdict=verdict, size_multiplier=Decimal("1") if allow else Decimal("0"),
        allow_new_capital=allow, reason="t",
        metrics={"avg_win_rate": win_rate, "profit_factor": str(pf)},
    )


def test_edge_kelly_trust_routes_capital_to_strongest_edge():
    reg = EdgeGateRegistry("/nonexistent/v.json").load()
    reg.set_verdict(_verdict_with_stats("trend_breakout", VERDICT_ALLOWED, True, 0.73, Decimal("4.0")))
    reg.set_verdict(_verdict_with_stats("mean_reversion", VERDICT_ALLOWED, True, 0.64, Decimal("1.9")))
    reg.set_verdict(_verdict_with_stats("toxic_short", VERDICT_BLOCKED, False, 0.30, Decimal("0.4")))
    trust = reg.edge_kelly_trust(
        neutral=Decimal("1"), max_trust=Decimal("1.5"), min_trust=Decimal("0.25"),
    )
    # Strongest proven edge pinned to the ceiling; weaker proven between
    # neutral and ceiling; every proven weapon stays >= neutral.
    assert trust["trend_breakout"] == Decimal("1.5")
    assert Decimal("1") < trust["mean_reversion"] < Decimal("1.5")
    # Denied (blocked) weapon floored.
    assert trust["toxic_short"] == Decimal("0.25")


def test_edge_kelly_trust_empty_when_nothing_proven():
    reg = EdgeGateRegistry("/nonexistent/v.json").load()
    reg.set_verdict(_verdict_with_stats("a", VERDICT_BLOCKED, False, 0.3, Decimal("0.5")))
    trust = reg.edge_kelly_trust(
        neutral=Decimal("1"), max_trust=Decimal("1.5"), min_trust=Decimal("0.25"),
    )
    # No positive edge → only the floored denied weapon, no proven mapping.
    assert trust == {"a": Decimal("0.25")}
