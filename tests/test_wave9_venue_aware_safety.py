"""
tests/test_wave9_venue_aware_safety.py
=======================================

D122 — dynamic regime-aware Wave 9 safety cushion.

The previous design pinned ``edge_to_cost_safety`` to a static ``2.0``
with a binary ``high_fee_edge_to_cost_safety = 1.3`` override above a
hardcoded ``high_fee_threshold_bps = 25.0``. That violated the project
rule that no operational threshold may be a frozen absolute. The new
gate computes safety per-candidate from:

    safety = base_anchor
           * (1 + risk_off_weight * (1 - market_state_score))
           * (1 + vol_weight      * max(0, vol_scalar - 1))
           * (1 + high_fee_lift   * max(0, (fee_bps - fee_anchor) / fee_anchor))
    clamp to [safety_min, safety_max]

These tests lock the dynamic behaviour: neutral regime ≈ base_anchor,
risk-off / high-vol / expensive-venue lift the cushion proportionally,
and the bounds clamp correctly.
"""

from __future__ import annotations

from execution.scheduler import Urgency
from execution.wave9_runtime import Wave9RuntimeConfig, pre_flight_cost_gate


def _cfg(**overrides) -> Wave9RuntimeConfig:
    raw = {
        "execution_models": {
            "enabled": True,
            "dynamic_safety": {
                "base_anchor": 1.2,
                "risk_off_weight": 0.8,
                "vol_weight": 0.5,
                "fee_anchor_bps": 5.0,
                "high_fee_lift": 0.0,
                "safety_min": 0.8,
                "safety_max": 2.5,
            },
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
    # Surface-level overrides apply to dynamic_safety for convenience.
    ds_overrides = {k: v for k, v in overrides.items() if k in {
        "base_anchor", "risk_off_weight", "vol_weight",
        "fee_anchor_bps", "high_fee_lift", "safety_min", "safety_max",
    }}
    if ds_overrides:
        raw["execution_models"]["dynamic_safety"].update(ds_overrides)
    return Wave9RuntimeConfig.from_dict(raw)


def _md(
    edge_bps: float,
    *,
    dv: float = 10_000_000.0,
    vol: float = 0.02,
    mss: float = 1.0,
    vol_scalar: float = 1.0,
) -> dict:
    return {
        "forecast_expected_return": edge_bps / 10_000.0,
        "daily_volume": dv,
        "daily_volatility": vol,
        "market_state_score": mss,
        "market_volatility_scalar": vol_scalar,
    }


# ── neutral regime → safety stays at base_anchor ─────────────────────────


def test_neutral_regime_uses_base_anchor() -> None:
    """mss=1.0, vol_scalar=1.0, low-fee venue → safety == base_anchor (1.2)."""
    cfg = _cfg()
    out = pre_flight_cost_gate(
        config=cfg, broker="ibkr", symbol="AAPL", asset_class="equity",
        quantity=100.0, signal_metadata=_md(edge_bps=20.0),
    )
    assert out.metadata["wave9_edge_to_cost_safety_applied"] == 1.2
    assert out.metadata["wave9_safety_base_anchor"] == 1.2
    assert out.metadata["wave9_safety_risk_off_lift"] == 0.0
    assert out.metadata["wave9_safety_vol_lift"] == 0.0


# ── risk-off lifts the cushion ──────────────────────────────────────────


def test_risk_off_regime_lifts_safety() -> None:
    """mss=0.0 (full risk-off) → safety = 1.2 * (1 + 0.8) = 2.16."""
    cfg = _cfg()
    out = pre_flight_cost_gate(
        config=cfg, broker="ibkr", symbol="AAPL", asset_class="equity",
        quantity=100.0, signal_metadata=_md(edge_bps=20.0, mss=0.0),
    )
    assert out.metadata["wave9_edge_to_cost_safety_applied"] == 2.16
    assert out.metadata["wave9_safety_risk_off_lift"] == 0.8


def test_partial_risk_off_scales_continuously() -> None:
    """mss=0.5 → lift = 0.8 * 0.5 = 0.4 → safety = 1.2 * 1.4 = 1.68."""
    cfg = _cfg()
    out = pre_flight_cost_gate(
        config=cfg, broker="ibkr", symbol="AAPL", asset_class="equity",
        quantity=100.0, signal_metadata=_md(edge_bps=20.0, mss=0.5),
    )
    assert abs(out.metadata["wave9_edge_to_cost_safety_applied"] - 1.68) < 1e-9


# ── volatility lifts the cushion ────────────────────────────────────────


def test_high_volatility_lifts_safety() -> None:
    """vol_scalar=3.0 → vol_lift = 0.5 * 2 = 1.0 → safety = 1.2 * 2.0 = 2.4."""
    cfg = _cfg()
    out = pre_flight_cost_gate(
        config=cfg, broker="ibkr", symbol="AAPL", asset_class="equity",
        quantity=100.0, signal_metadata=_md(edge_bps=20.0, vol_scalar=3.0),
    )
    assert abs(out.metadata["wave9_edge_to_cost_safety_applied"] - 2.4) < 1e-9


def test_low_volatility_below_one_does_not_lift() -> None:
    """vol_scalar < 1.0 floors the lift at 0 — calm markets don't lift."""
    cfg = _cfg()
    out = pre_flight_cost_gate(
        config=cfg, broker="ibkr", symbol="AAPL", asset_class="equity",
        quantity=100.0, signal_metadata=_md(edge_bps=20.0, vol_scalar=0.5),
    )
    assert out.metadata["wave9_safety_vol_lift"] == 0.0


# ── high-fee venue (when high_fee_lift > 0) ─────────────────────────────


def test_high_fee_lift_zero_by_default_no_lift() -> None:
    """Default high_fee_lift=0 → kraken (40 bps) gets no fee-driven lift."""
    cfg = _cfg()
    out = pre_flight_cost_gate(
        config=cfg, broker="kraken", symbol="BTC-USD", asset_class="crypto",
        quantity=1.0, signal_metadata=_md(edge_bps=80.0),
    )
    assert out.metadata["wave9_safety_fee_lift"] == 0.0
    # Neutral regime, no fee lift → safety == base_anchor.
    assert out.metadata["wave9_edge_to_cost_safety_applied"] == 1.2


def test_high_fee_lift_active_scales_with_fee_ratio() -> None:
    """high_fee_lift=0.2, fee_anchor=5, kraken=40 → ratio = 7, lift = 1.4 → safety = 1.2*2.4 = 2.88, clamped to 2.5."""
    cfg = _cfg(high_fee_lift=0.2)
    out = pre_flight_cost_gate(
        config=cfg, broker="kraken", symbol="BTC-USD", asset_class="crypto",
        quantity=1.0, signal_metadata=_md(edge_bps=80.0),
    )
    assert out.metadata["wave9_safety_fee_lift"] > 0.0
    # Clamped at safety_max = 2.5.
    assert out.metadata["wave9_edge_to_cost_safety_applied"] == 2.5


# ── clamps ──────────────────────────────────────────────────────────────


def test_safety_clamped_at_min() -> None:
    """base_anchor=0.5 < safety_min=0.8 → clamp to 0.8."""
    cfg = _cfg(base_anchor=0.5)
    out = pre_flight_cost_gate(
        config=cfg, broker="ibkr", symbol="AAPL", asset_class="equity",
        quantity=100.0, signal_metadata=_md(edge_bps=20.0),
    )
    assert out.metadata["wave9_edge_to_cost_safety_applied"] == 0.8


def test_safety_clamped_at_max() -> None:
    """Extreme risk-off + high vol piled together → clamp at safety_max=2.5."""
    cfg = _cfg()
    out = pre_flight_cost_gate(
        config=cfg, broker="ibkr", symbol="AAPL", asset_class="equity",
        quantity=100.0, signal_metadata=_md(edge_bps=20.0, mss=0.0, vol_scalar=5.0),
    )
    assert out.metadata["wave9_edge_to_cost_safety_applied"] == 2.5


# ── behaviour: thin-edge trades on expensive venues clear when ──────────
# ── regime is calm + slider implies "deploy" intent (low base anchor) ──


def test_low_anchor_lets_thin_edge_kraken_trade_clear() -> None:
    """With base_anchor=1.0 (cost = edge break-even), a slightly-positive
    edge on Kraken clears — the old static 2.0 cushion would have blocked.
    """
    cfg = _cfg(base_anchor=1.0)
    out = pre_flight_cost_gate(
        config=cfg, broker="kraken", symbol="BTC-USD", asset_class="crypto",
        quantity=1.0, signal_metadata=_md(edge_bps=90.0),
    )
    assert out.allow is True
    assert out.urgency is not Urgency.DO_NOT_TRADE


# ── metadata contract ───────────────────────────────────────────────────


def test_metadata_audit_fields_present() -> None:
    cfg = _cfg()
    out = pre_flight_cost_gate(
        config=cfg, broker="kraken", symbol="ETH-USD", asset_class="crypto",
        quantity=10.0, signal_metadata=_md(edge_bps=60.0),
    )
    for key in (
        "wave9_edge_to_cost_safety_applied",
        "wave9_safety_base_anchor",
        "wave9_safety_risk_off_lift",
        "wave9_safety_vol_lift",
        "wave9_safety_fee_lift",
        "wave9_market_state_score",
        "wave9_market_volatility_scalar",
        "wave9_fee_bps",
    ):
        assert key in out.metadata, f"missing {key} in {out.metadata}"


# ── proxy ceiling (unchanged from Wave 9 hardening) ─────────────────────


def test_score_proxy_ceiling_unchanged_by_dynamic_safety() -> None:
    """The conviction→edge proxy ceiling is independent of safety; still
    max(200, 4*fee_bps) so score-only signals are not penalised by the
    safety refactor.
    """
    cfg = _cfg()
    score_only_md = {
        "expected_edge": 1.0,  # max conviction
        "daily_volume": 10_000_000.0,
        "daily_volatility": 0.02,
        "market_state_score": 1.0,
        "market_volatility_scalar": 1.0,
    }
    ibkr_out = pre_flight_cost_gate(
        config=cfg, broker="ibkr", symbol="AAPL", asset_class="equity",
        quantity=100.0, signal_metadata=score_only_md,
    )
    kraken_out = pre_flight_cost_gate(
        config=cfg, broker="kraken", symbol="ETH-USD", asset_class="crypto",
        quantity=1.0, signal_metadata=score_only_md,
    )
    assert ibkr_out.metadata["wave9_edge_bps"] == 200.0
    assert kraken_out.metadata["wave9_edge_bps"] == 200.0
