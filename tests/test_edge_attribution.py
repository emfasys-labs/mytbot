"""
tests/test_edge_attribution.py
===============================
Per-bucket / per-symbol net-of-cost evidence governor.

Pins the strict auto-recovering behaviour the operator chose: a
persistently money-losing bucket/symbol faces a steeply widened edge
bar that snaps back to normal the moment its rolling net-of-cost
attribution turns positive — fully dynamic, no hardcoded disable.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal

from portfolio.global_edge_coordinator import GlobalEdgeCoordinator, HeldPositionEdge
from portfolio.strategy_opportunity import StrategyOpportunity, compute_priority_score
from system.edge_attribution import (
    compute_edge_attribution,
    governor_multiplier,
    normalise_bucket,
    required_threshold_multiplier,
)


# ── Bucket normalisation ────────────────────────────────────────────────


def test_normalise_bucket_collapses_churn_engines() -> None:
    assert normalise_bucket("GlobalEdge Rotation") == "global_edge_rotation"
    assert normalise_bucket("capital_recycle_dead_edge") == "capital_recycle"
    assert normalise_bucket("x", "trim_symbol") == "global_edge_trim"
    assert normalise_bucket("adaptive_shed_to_target") == "adaptive_shed"
    assert normalise_bucket("mean_reversion") == "mean_reversion"
    assert normalise_bucket("", None) == "unknown"


# ── Governor multiplier: strict + auto-recovering + monotone ────────────


def test_proven_positive_is_full_freedom() -> None:
    assert governor_multiplier(500.0, 30) == 1.0
    assert governor_multiplier(0.0, 30) == 1.0


def test_severe_bleed_is_capped_max() -> None:
    # |net| >= ref_loss (1500 default) and enough samples → MAX (8.0).
    assert governor_multiplier(-1500.0, 30) == 8.0
    assert governor_multiplier(-99999.0, 30) == 8.0  # clamped, never explodes


def test_unproven_is_cautious_not_punitive() -> None:
    # Thin sample (positive OR negative) → the cautious unproven bar.
    assert governor_multiplier(-50.0, 2) == 1.5
    assert governor_multiplier(800.0, 1) == 1.5


def test_governor_is_monotone_more_loss_never_relaxes() -> None:
    prev = 0.0
    for net in (1000.0, 0.0, -100.0, -500.0, -1000.0, -1500.0, -5000.0):
        cur = governor_multiplier(net, 30)
        assert cur >= prev or net >= 0.0, (net, cur, prev)
        prev = cur if net < 0 else prev


def test_auto_recovery_flip_to_positive_restores_freedom() -> None:
    bleeding = governor_multiplier(-1200.0, 30)
    recovered = governor_multiplier(+10.0, 30)  # same path, now net-positive
    assert bleeding > 1.0 and recovered == 1.0


# ── Worst-offender selection ────────────────────────────────────────────


def test_worst_of_symbol_or_bucket_governs() -> None:
    attr = {
        "buckets": {"volatility_regime": {"net": 200.0, "n": 40}},  # good
        "symbols": {"ETH-USD": {"net": -1500.0, "n": 40}},          # bleeding
    }
    # Good strategy, bleeding symbol → still throttled hard.
    assert required_threshold_multiplier("ETH-USD", "volatility_regime", attr) == 8.0
    # Clean symbol on the same good strategy → untouched.
    assert required_threshold_multiplier("SOL-USD", "volatility_regime", attr) == 1.0


def test_absent_attribution_is_noop() -> None:
    assert required_threshold_multiplier("ETH-USD", "x", None) == 1.0
    assert required_threshold_multiplier("ETH-USD", "x", {}) == 1.0


# ── Rolling reconstruction (net of fees) ────────────────────────────────


class _Row:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _FakeSession:
    """Returns OrderLog rows for the first execute, signal rows for the
    second — enough to drive compute_edge_attribution deterministically."""

    def __init__(self, orders, sig_rows):
        self._orders = orders
        self._sig_rows = sig_rows
        self._calls = 0

    async def execute(self, _stmt):
        self._calls += 1
        if self._calls == 1:
            return _Result(self._orders)
        return _Result(self._sig_rows)


def test_compute_attribution_nets_fees_and_attributes_bucket_symbol() -> None:
    now = datetime(2026, 5, 17, 12, 0, tzinfo=timezone.utc)
    ts = datetime(2026, 5, 17, 10, 0, tzinfo=timezone.utc)
    # Open 10 @ 100 (buy), then close 10 @ 90 (sell) → gross -100, fee 2 →
    # net -102, attributed to the CLOSING order's strategy bucket + symbol.
    orders = [
        _Row(broker="kraken", symbol="ETH-USD", side="buy", filled_quantity=10,
             quantity=10, avg_fill_price=100, limit_price=100, fee=1,
             status="filled", timestamp=ts, id=1, signal_id="s1"),
        _Row(broker="kraken", symbol="ETH-USD", side="sell", filled_quantity=10,
             quantity=10, avg_fill_price=90, limit_price=90, fee=2,
             status="filled", timestamp=ts, id=2, signal_id="s2"),
    ]
    sig_rows = [("s1", "volatility_regime"), ("s2", "capital_recycle")]
    out = asyncio.run(
        compute_edge_attribution(
            _FakeSession(orders, sig_rows), window_days=4.0, now=now
        )
    )
    assert out["orders"] == 2
    # Close attributed to s2 → bucket capital_recycle, symbol ETH-USD.
    assert round(out["buckets"]["capital_recycle"]["net"], 2) == -102.0
    assert round(out["symbols"]["ETH-USD"]["net"], 2) == -102.0
    assert out["buckets"]["capital_recycle"]["n"] == 1.0


def test_compute_attribution_empty_is_safe() -> None:
    out = asyncio.run(
        compute_edge_attribution(_FakeSession([], []), window_days=4.0)
    )
    assert out == {
        "buckets": {},
        "symbols": {},
        "window_days": 4.0,
        "computed_at": out["computed_at"],
        "orders": 0,
    }


# ── End-to-end: the coordinator gate actually throttles a bleeder ───────


def _opp(symbol: str, edge: str, strat: str = "momentum_breakout") -> StrategyOpportunity:
    e = Decimal(edge)
    return StrategyOpportunity(
        strategy_name=strat,
        symbol=symbol,
        side="long",
        created_at=datetime.now(timezone.utc),
        expected_edge=e,
        confidence=Decimal("0.9"),
        capital_required=Decimal("10000"),
        expected_holding_hours=24,
        liquidity_score=Decimal("0.8"),
        execution_score=Decimal("0.8"),
        regime_fit_score=Decimal("0.85"),
        risk_cost_score=Decimal("0.05"),
        priority_score=compute_priority_score(
            e, Decimal("0.9"), Decimal("0.85"), Decimal("0.8"), Decimal("0.05")
        ),
        metadata={},
    )


def _base_cfg(attr=None):
    cfg = {
        "edge_advantage": {"trader": "0.05"},
        "max_actions_per_tick": 3,
        "max_notional_fraction_per_action": "1.0",
    }
    if attr is not None:
        cfg["edge_attribution"] = attr
    return cfg


def test_governor_filters_bleeding_symbol_but_not_clean_one() -> None:
    held = [
        HeldPositionEdge(
            symbol="AAA", notional=Decimal("1000"),
            expected_remaining_edge=Decimal("0.20"),
        )
    ]
    # Edge 0.30 clears the normal bar (0.20 weakest + 0.05 thresh = 0.25).
    clean = GlobalEdgeCoordinator(_base_cfg()).propose_actions(
        held, [_opp("BBB", "0.30")], active_mode="trader"
    )
    assert clean, "clean opp must pass the normal bar"

    # Same opp, but BBB is a proven net-loser → bar widens 8× (0.05→0.40,
    # so it now needs > 0.60) → filtered.
    attr = {"buckets": {}, "symbols": {"BBB": {"net": -1500.0, "n": 30}}}
    throttled = GlobalEdgeCoordinator(_base_cfg(attr)).propose_actions(
        held, [_opp("BBB", "0.30")], active_mode="trader"
    )
    assert throttled == [], "bleeding symbol must be throttled out"

    # A different, clean symbol on the same run is unaffected.
    ok = GlobalEdgeCoordinator(_base_cfg(attr)).propose_actions(
        held, [_opp("CCC", "0.30")], active_mode="trader"
    )
    assert ok, "clean symbol must still pass under the same attribution"
