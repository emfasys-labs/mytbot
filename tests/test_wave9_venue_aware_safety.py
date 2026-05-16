"""
tests/test_wave9_venue_aware_safety.py
=======================================

Locks in the venue-aware ``edge_to_cost_safety`` relaxation: on high-fee
venues (>= ``high_fee_threshold_bps`` taker), the gate swaps in a relaxed
safety multiplier instead of the default 2.0 cushion. Without this,
40 bps-fee venues like Kraken get locked out of execution entirely.
"""

from __future__ import annotations

from execution.scheduler import Urgency
from execution.wave9_runtime import Wave9RuntimeConfig, pre_flight_cost_gate


def _cfg(**overrides) -> Wave9RuntimeConfig:
    raw = {
        "execution_models": {
            "enabled": True,
            "high_fee_threshold_bps": 25.0,
            "high_fee_edge_to_cost_safety": 1.3,
            "urgency_policy": {
                "market_cost_ceiling": 8.0,
                "limit_cost_ceiling": 25.0,
                "passive_cost_ceiling": 60.0,
                "do_not_trade_ceiling": 150.0,
                "edge_to_cost_safety": 2.0,
                "high_urgency_multiplier": 1.5,
                "high_urgency_threshold": 0.8,
            },
            "venue_priors": {
                "fees": {
                    "ibkr":   {"taker_bps": 1.0,  "maker_bps": 0.0},
                    "kraken": {"taker_bps": 40.0, "maker_bps": 16.0},
                    "alpaca": {"taker_bps": 0.0,  "maker_bps": 0.0},
                },
                "spreads": {
                    "ibkr":   {"equity": 1.0},
                    "kraken": {"crypto": 8.0},
                    "alpaca": {"equity": 1.0},
                },
            },
        }
    }
    raw["execution_models"].update(overrides)
    return Wave9RuntimeConfig.from_dict(raw)


def _md(edge_bps: float, dv: float = 10_000_000.0, vol: float = 0.02) -> dict:
    return {
        # ``forecast_expected_return`` is in fractional form; *10_000 → bps
        "forecast_expected_return": edge_bps / 10_000.0,
        "daily_volume": dv,
        "daily_volatility": vol,
    }


def test_low_fee_venue_keeps_strict_safety() -> None:
    """IBKR at 1 bps fee → relaxation must NOT kick in."""
    cfg = _cfg()
    out = pre_flight_cost_gate(
        config=cfg, broker="ibkr", symbol="AAPL", asset_class="equity",
        quantity=100.0, signal_metadata=_md(edge_bps=20.0),
    )
    assert out.metadata["wave9_venue_relaxed"] is False
    assert out.metadata["wave9_edge_to_cost_safety_applied"] == 2.0


def test_high_fee_venue_applies_relaxed_safety() -> None:
    """Kraken at 40 bps fee → effective safety drops to 1.3."""
    cfg = _cfg()
    out = pre_flight_cost_gate(
        config=cfg, broker="kraken", symbol="BTC-USD", asset_class="crypto",
        quantity=1.0, signal_metadata=_md(edge_bps=80.0),
    )
    assert out.metadata["wave9_venue_relaxed"] is True
    assert out.metadata["wave9_edge_to_cost_safety_applied"] == 1.3
    assert out.metadata["wave9_fee_bps"] >= 25.0


def test_high_fee_venue_lets_kraken_trade_when_static_safety_would_block() -> None:
    """Edge that fails 2.0x cushion but clears 1.3x must now pass."""
    cfg = _cfg()
    # Build edge that's > 1.3 * cost but < 2.0 * cost on Kraken.
    # Empirically Kraken cost lands ~55 bps for this size+vol; pick 90 bps.
    edge_bps = 90.0
    out = pre_flight_cost_gate(
        config=cfg, broker="kraken", symbol="BTC-USD", asset_class="crypto",
        quantity=1.0, signal_metadata=_md(edge_bps=edge_bps),
    )
    # Confirm relaxed path actually let it through.
    assert out.metadata["wave9_venue_relaxed"] is True
    # Allow either ok or non-DO_NOT_TRADE — the key is the gate didn't veto.
    assert out.allow is True
    assert out.urgency is not Urgency.DO_NOT_TRADE


def test_low_fee_venue_with_same_marginal_edge_still_clears_at_strict() -> None:
    """Sanity: on IBKR the strict 2.0 safety still trades when edge dominates."""
    cfg = _cfg()
    out = pre_flight_cost_gate(
        config=cfg, broker="ibkr", symbol="AAPL", asset_class="equity",
        quantity=100.0, signal_metadata=_md(edge_bps=50.0),
    )
    assert out.allow is True
    assert out.metadata["wave9_venue_relaxed"] is False


def test_relaxation_disabled_when_threshold_above_all_fees() -> None:
    """If threshold is set high enough that no venue qualifies, no relaxation."""
    cfg = _cfg(high_fee_threshold_bps=200.0)
    out = pre_flight_cost_gate(
        config=cfg, broker="kraken", symbol="BTC-USD", asset_class="crypto",
        quantity=1.0, signal_metadata=_md(edge_bps=80.0),
    )
    assert out.metadata["wave9_venue_relaxed"] is False
    assert out.metadata["wave9_edge_to_cost_safety_applied"] == 2.0


def test_relaxation_disabled_when_relaxed_safety_is_zero() -> None:
    """Setting ``high_fee_edge_to_cost_safety: 0`` opts out of the override."""
    cfg = _cfg(high_fee_edge_to_cost_safety=0.0)
    out = pre_flight_cost_gate(
        config=cfg, broker="kraken", symbol="BTC-USD", asset_class="crypto",
        quantity=1.0, signal_metadata=_md(edge_bps=80.0),
    )
    assert out.metadata["wave9_venue_relaxed"] is False


def test_score_proxy_ceiling_scales_with_high_fee_venue() -> None:
    """Score-only signals must get a higher edge proxy on high-fee venues."""
    cfg = _cfg()
    # Conviction-only metadata (no ``forecast_expected_return``)
    score_only_md = {
        "expected_edge": 1.0,  # max conviction
        "daily_volume": 10_000_000.0,
        "daily_volatility": 0.02,
    }
    ibkr_out = pre_flight_cost_gate(
        config=cfg, broker="ibkr", symbol="AAPL", asset_class="equity",
        quantity=100.0, signal_metadata=score_only_md,
    )
    kraken_out = pre_flight_cost_gate(
        config=cfg, broker="kraken", symbol="ETH-USD", asset_class="crypto",
        quantity=1.0, signal_metadata=score_only_md,
    )
    # Wave-9 lockout fix (#2): the uncalibrated conviction→edge proxy ceiling
    # was raised from max(25, 2*fee) → max(200, 4*fee) bps. At cap-clipping
    # conviction (1.0) the proxy == the ceiling. The previous 25 bps cap made
    # every score-only trade permanently fail the cost cushion once the book
    # filled (the documented lockout). Floor now dominates ≤ 50 bps fee;
    # venue scaling still applies above that.
    assert ibkr_out.metadata["wave9_edge_bps"] == 200.0  # max(200, 4*1)
    assert kraken_out.metadata["wave9_edge_bps"] == 200.0  # max(200, 4*40)


def test_metadata_audit_fields_present() -> None:
    cfg = _cfg()
    out = pre_flight_cost_gate(
        config=cfg, broker="kraken", symbol="ETH-USD", asset_class="crypto",
        quantity=10.0, signal_metadata=_md(edge_bps=60.0),
    )
    for key in (
        "wave9_edge_to_cost_safety_applied",
        "wave9_venue_relaxed",
        "wave9_fee_bps",
    ):
        assert key in out.metadata, f"missing {key} in {out.metadata}"
