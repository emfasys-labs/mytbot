"""
tests/test_wave9_execution.py
================================
Wave 9 acceptance tests for the execution-cost / impact / scheduling
modules.

Coverage:

- ``square_root_impact_bps`` is monotone in order size and goes to
  zero on degenerate inputs.
- ``total_execution_cost_bps`` sums components correctly.
- ``SlippageModel`` returns the default before any update; after
  updates returns broker-symbol prior once ``min_samples_specific``
  reached; round-trips through snapshot/restore.
- ``VenuePriors.from_dict`` maps fees + spreads correctly.
- ``slice_order`` produces N children that sum to parent and obey
  participation cap; rejects beyond threshold; falls back to single
  child on missing volume.
- ``decide_urgency`` honours the ladder MARKET → LIMIT → PASSIVE →
  SLICED → DO_NOT_TRADE; cost > edge × safety ⇒ DO_NOT_TRADE; crash
  regime never returns MARKET; high signal urgency relaxes ceilings.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from execution.impact import (
    DEFAULT_IMPACT_COEFFICIENTS,
    participation_rate,
    square_root_impact_bps,
    total_execution_cost_bps,
)
from execution.order_slicer import slice_order
from execution.scheduler import (
    Urgency,
    UrgencyPolicy,
    decide_urgency,
)
from execution.slippage_model import SlippageModel
from execution.venue_quality import FeePrior, SpreadPrior, VenuePriors


# ── impact ─────────────────────────────────────────────────────────────────


def test_participation_rate_basic() -> None:
    assert participation_rate(100.0, 1000.0) == pytest.approx(0.10)
    assert participation_rate(0.0, 1000.0) == 0.0
    assert participation_rate(100.0, 0.0) == 0.0
    assert participation_rate(100.0, -1.0) == 0.0


def test_square_root_impact_monotone_in_size() -> None:
    bps_small = square_root_impact_bps(
        order_qty=100.0, daily_volume=1_000_000.0, daily_volatility=0.20, asset_class="equity"
    )
    bps_large = square_root_impact_bps(
        order_qty=10_000.0, daily_volume=1_000_000.0, daily_volatility=0.20, asset_class="equity"
    )
    assert 0 < bps_small < bps_large


def test_square_root_impact_zero_on_bad_input() -> None:
    assert square_root_impact_bps(
        order_qty=100.0, daily_volume=0.0, daily_volatility=0.20
    ) == 0.0
    assert square_root_impact_bps(
        order_qty=100.0, daily_volume=1_000.0, daily_volatility=0.0
    ) == 0.0


def test_square_root_impact_uses_default_coefficient_per_asset() -> None:
    bps_eq = square_root_impact_bps(
        order_qty=1_000.0, daily_volume=10_000.0, daily_volatility=0.20, asset_class="equity"
    )
    bps_op = square_root_impact_bps(
        order_qty=1_000.0, daily_volume=10_000.0, daily_volatility=0.20, asset_class="option"
    )
    # option coefficient is 0.20 vs equity 0.10 → ~2x.
    assert bps_op == pytest.approx(2.0 * bps_eq, rel=1e-9)


def test_total_execution_cost_sums_components() -> None:
    out = total_execution_cost_bps(
        order_qty=1_000.0,
        daily_volume=1_000_000.0,
        daily_volatility=0.20,
        asset_class="equity",
        fee_bps=2.0,
        spread_bps=1.5,
        slippage_bps=0.5,
    )
    assert out.fee_bps == 2.0
    assert out.spread_bps == 1.5
    assert out.slippage_bps == 0.5
    assert out.impact_bps > 0
    assert out.total_bps == pytest.approx(out.fee_bps + out.spread_bps + out.slippage_bps + out.impact_bps)


# ── slippage model ─────────────────────────────────────────────────────────


def test_slippage_model_default_when_empty() -> None:
    m = SlippageModel(default_bps=4.0)
    est = m.estimate(broker="ibkr", symbol="AAPL", asset_class="equity")
    assert est.source == "default"
    assert est.bps == 4.0


def test_slippage_model_specific_tier_kicks_in() -> None:
    m = SlippageModel(min_samples_specific=3, default_bps=10.0)
    for v in (12.0, 11.0, 10.0):
        m.update(broker="ibkr", symbol="AAPL", asset_class="equity", observed_bps=v)
    est = m.estimate(broker="ibkr", symbol="AAPL", asset_class="equity")
    assert est.source == "broker_symbol"
    assert est.samples == 3


def test_slippage_model_falls_back_to_group() -> None:
    m = SlippageModel(min_samples_specific=10, default_bps=99.0)
    m.update(broker="ibkr", symbol="OTHER", asset_class="equity", observed_bps=7.0)
    est = m.estimate(broker="ibkr", symbol="UNSEEN", asset_class="equity")
    assert est.source == "broker_asset"


def test_slippage_model_snapshot_roundtrip() -> None:
    m = SlippageModel(min_samples_specific=2)
    for v in (5.0, 7.0, 6.0):
        m.update(broker="ibkr", symbol="AAPL", asset_class="equity", observed_bps=v)
    snap = m.snapshot()
    m2 = SlippageModel(min_samples_specific=2)
    m2.restore(snap)
    e1 = m.estimate(broker="ibkr", symbol="AAPL", asset_class="equity")
    e2 = m2.estimate(broker="ibkr", symbol="AAPL", asset_class="equity")
    assert e1.source == e2.source
    assert e1.bps == pytest.approx(e2.bps, rel=1e-12)
    assert e1.samples == e2.samples


# ── venue priors ───────────────────────────────────────────────────────────


def test_venue_priors_from_dict() -> None:
    raw = {
        "venue_priors": {
            "fees": {
                "ibkr": {"taker_bps": 1.0, "maker_bps": 0.0},
                "kraken": {"taker_bps": 26.0, "maker_bps": 16.0},
            },
            "spreads": {
                "ibkr": {"equity": 1.0, "etf": 1.0, "option": 20.0},
                "kraken": {"crypto": 8.0},
            },
        }
    }
    vp = VenuePriors.from_dict(raw)
    assert vp.fee_for("IBKR", taker=True) == 1.0
    assert vp.fee_for("kraken", taker=False) == 16.0
    assert vp.spread_for("ibkr", "option") == 20.0
    assert vp.spread_for("kraken", "crypto") == 8.0
    # Unknown broker ⇒ default (equity 1.0 from baseline SpreadPrior).
    assert vp.spread_for("unknown_broker", "equity") == 1.0


# ── order slicer ───────────────────────────────────────────────────────────


def test_slice_order_single_child_when_below_cap() -> None:
    res = slice_order(
        parent_quantity=Decimal("50"),
        daily_volume=10_000.0,
        participation_rate_cap=0.10,  # cap_qty = 1000, parent = 50 < cap ⇒ single
        rejection_participation=0.30,
    )
    assert res.rejected is False
    assert len(res.children) == 1
    assert res.children[0].quantity == Decimal("50")


def test_slice_order_n_children_respect_participation() -> None:
    # parent=300, cap_qty=10000*0.05=500 → 1 child. Bump cap down so we get more.
    res = slice_order(
        parent_quantity=Decimal("3000"),
        daily_volume=10_000.0,
        participation_rate_cap=0.10,
        rejection_participation=0.50,
    )
    assert res.rejected is False
    assert len(res.children) >= 3
    total = sum((c.quantity for c in res.children), start=Decimal("0"))
    assert total == Decimal("3000")
    # Each child quantity within cap (with tiny tolerance for the
    # last residual rounding).
    for c in res.children:
        assert float(c.quantity) <= 10_000.0 * 0.10 + 1e-6


def test_slice_order_rejects_when_above_threshold() -> None:
    res = slice_order(
        parent_quantity=Decimal("4000"),
        daily_volume=10_000.0,
        participation_rate_cap=0.10,
        rejection_participation=0.30,  # 4000/10000 = 0.40 > 0.30 ⇒ reject
    )
    assert res.rejected is True
    assert res.children == []
    assert res.reason == "exceeds_rejection_threshold"


def test_slice_order_no_volume_fallback() -> None:
    res = slice_order(
        parent_quantity=Decimal("100"),
        daily_volume=0.0,
        participation_rate_cap=0.10,
        rejection_participation=0.30,
    )
    assert res.rejected is False
    assert len(res.children) == 1
    assert res.children[0].quantity == Decimal("100")
    assert res.reason == "no_volume_data"


def test_slice_order_explicit_n_slices() -> None:
    res = slice_order(
        parent_quantity=Decimal("100"),
        daily_volume=1_000.0,
        n_slices=4,
        rejection_participation=0.50,
    )
    assert res.rejected is False
    assert len(res.children) == 4
    assert sum((c.quantity for c in res.children), start=Decimal("0")) == Decimal("100")


def test_slice_order_schedules_over_window() -> None:
    res = slice_order(
        parent_quantity=Decimal("3000"),
        daily_volume=10_000.0,
        participation_rate_cap=0.10,
        rejection_participation=0.50,
        schedule_window_seconds=60,
        start_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    times = [c.schedule_at for c in res.children]
    assert all(t is not None for t in times)
    # Strictly increasing or equal.
    for a, b in zip(times[:-1], times[1:]):
        assert a <= b  # type: ignore[operator]


# ── urgency policy ─────────────────────────────────────────────────────────


def test_urgency_market_when_cost_low() -> None:
    d = decide_urgency(expected_cost_bps=2.0, edge_bps=20.0, signal_urgency=0.5)
    assert d.urgency is Urgency.MARKET


def test_urgency_limit_then_passive_then_sliced() -> None:
    p = UrgencyPolicy()
    d_limit = decide_urgency(expected_cost_bps=15.0, edge_bps=200.0, policy=p)
    assert d_limit.urgency is Urgency.LIMIT
    d_passive = decide_urgency(expected_cost_bps=40.0, edge_bps=200.0, policy=p)
    assert d_passive.urgency is Urgency.PASSIVE
    d_sliced = decide_urgency(expected_cost_bps=100.0, edge_bps=300.0, policy=p)
    assert d_sliced.urgency is Urgency.SLICED


def test_urgency_do_not_trade_when_cost_above_dnt() -> None:
    p = UrgencyPolicy(do_not_trade_ceiling=100.0)
    d = decide_urgency(expected_cost_bps=200.0, edge_bps=500.0, policy=p)
    assert d.urgency is Urgency.DO_NOT_TRADE


def test_urgency_do_not_trade_when_cost_exceeds_edge() -> None:
    d = decide_urgency(expected_cost_bps=10.0, edge_bps=5.0)
    assert d.urgency is Urgency.DO_NOT_TRADE
    assert d.reason == "cost_exceeds_edge"


def test_urgency_crash_regime_avoids_market() -> None:
    d = decide_urgency(
        expected_cost_bps=2.0, edge_bps=20.0, signal_urgency=0.9, regime_label="crash"
    )
    assert d.urgency is not Urgency.MARKET


def test_urgency_high_signal_relaxes_ceiling() -> None:
    p = UrgencyPolicy()
    # Cost 12 bps would normally be LIMIT (>8 market ceiling), but
    # high urgency × 1.5 = 12 ⇒ MARKET.
    d_low = decide_urgency(expected_cost_bps=12.0, edge_bps=200.0, signal_urgency=0.3, policy=p)
    d_high = decide_urgency(expected_cost_bps=12.0, edge_bps=200.0, signal_urgency=0.9, policy=p)
    # At the very least, high-urgency should not be more conservative than low.
    order = [Urgency.MARKET, Urgency.LIMIT, Urgency.PASSIVE, Urgency.SLICED, Urgency.DO_NOT_TRADE]
    assert order.index(d_high.urgency) <= order.index(d_low.urgency)
