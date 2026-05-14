"""
tests/test_adaptive_edge.py
============================
Phase 2 — cost-aware edge threshold.

The threshold replaces a static per-mode number with a function of:
  * live execution cost (fee + spread + slippage)
  * recent realised outcomes (win-rate, average return)
  * mode bias (hunter most aggressive)

These tests pin the formula behaviour and the safety floor.
"""

from __future__ import annotations

from decimal import Decimal

from system.adaptive_edge import (
    EdgeThresholdInputs,
    compute_edge_threshold,
    estimate_cross_venue_cost_bps,
)


# ── Static floor is never crossed ───────────────────────────────────────


def test_static_floor_is_respected_when_cost_is_low() -> None:
    """Cheap venue + no outcomes → threshold defaults to operator's floor."""
    out = compute_edge_threshold(
        EdgeThresholdInputs(
            mode="hunter",
            cross_venue_cost_bps=1.0,  # 1bp — practically nothing
            static_floor=0.02,
        )
    )
    # Without floor: 2 × 1 × 1.0 / 10000 = 0.0002. Floor wins.
    assert out == Decimal("0.02")


def test_threshold_exceeds_floor_when_cost_is_high() -> None:
    """High-fee venue → threshold reflects the round-trip cost."""
    out = compute_edge_threshold(
        EdgeThresholdInputs(
            mode="hunter",
            cross_venue_cost_bps=200.0,  # 2% per round trip cost
            static_floor=0.02,
        )
    )
    # 2 × 200 × 1.0 / 10000 = 0.04 > floor 0.02
    assert out == Decimal("0.04")


# ── Mode bias ───────────────────────────────────────────────────────────


def test_defender_demands_more_edge_than_hunter() -> None:
    cost_bps = 50.0
    h = compute_edge_threshold(EdgeThresholdInputs(mode="hunter", cross_venue_cost_bps=cost_bps, static_floor=0.0))
    t = compute_edge_threshold(EdgeThresholdInputs(mode="trader", cross_venue_cost_bps=cost_bps, static_floor=0.0))
    d = compute_edge_threshold(EdgeThresholdInputs(mode="defender", cross_venue_cost_bps=cost_bps, static_floor=0.0))
    assert h < t < d


# ── Outcome cushion ─────────────────────────────────────────────────────


def test_winning_streak_shrinks_threshold() -> None:
    base = compute_edge_threshold(
        EdgeThresholdInputs(mode="hunter", cross_venue_cost_bps=50.0, static_floor=0.0)
    )
    winning = compute_edge_threshold(
        EdgeThresholdInputs(
            mode="hunter",
            cross_venue_cost_bps=50.0,
            recent_win_rate=0.7,
            recent_avg_return=0.005,
            static_floor=0.0,
        )
    )
    assert winning < base


def test_losing_streak_expands_threshold() -> None:
    base = compute_edge_threshold(
        EdgeThresholdInputs(mode="hunter", cross_venue_cost_bps=50.0, static_floor=0.0)
    )
    losing = compute_edge_threshold(
        EdgeThresholdInputs(
            mode="hunter",
            cross_venue_cost_bps=50.0,
            recent_win_rate=0.3,
            recent_avg_return=-0.002,
            static_floor=0.0,
        )
    )
    assert losing > base


# ── Robustness ──────────────────────────────────────────────────────────


def test_missing_cost_uses_default_baseline() -> None:
    out = compute_edge_threshold(
        EdgeThresholdInputs(mode="hunter", cross_venue_cost_bps=None, static_floor=0.0)
    )
    # Default cost (10 bps) × 2 × 1.0 / 10000 = 0.002
    assert out == Decimal("0.002")


def test_zero_cost_is_treated_as_default_not_zero() -> None:
    out = compute_edge_threshold(
        EdgeThresholdInputs(mode="hunter", cross_venue_cost_bps=0.0, static_floor=0.0)
    )
    assert out > Decimal("0")


# ── Cost estimator ──────────────────────────────────────────────────────


class _FakePriors:
    def fee_for(self, broker, taker=True):
        return {"ibkr": 1.0, "kraken": 26.0, "alpaca": 0.0}.get(broker, 5.0)
    def spread_for(self, broker, ac):
        return {"equity": 1.0, "crypto": 5.0}.get(ac, 2.0)


class _SlipEst:
    def __init__(self, bps): self.bps = bps


class _FakeSlip:
    def estimate(self, broker, symbol, asset_class):
        return _SlipEst(3.0)


def test_estimate_cross_venue_cost_averages_active_combos() -> None:
    cost = estimate_cross_venue_cost_bps(
        venue_priors=_FakePriors(),
        slippage_model=_FakeSlip(),
        active_brokers=["ibkr", "alpaca"],
        active_asset_classes=["equity"],
    )
    # ibkr equity: 1 + 1 + 3 = 5;  alpaca equity: 0 + 1 + 3 = 4
    # mean: 4.5
    assert cost == 4.5


def test_estimate_cross_venue_cost_returns_none_when_no_inputs() -> None:
    assert estimate_cross_venue_cost_bps(
        venue_priors=None, slippage_model=None,
        active_brokers=[], active_asset_classes=[],
    ) is None
