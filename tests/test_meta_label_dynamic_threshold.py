"""
tests/test_meta_label_dynamic_threshold.py
===========================================

D122 — dynamic, calibration-anchored meta-label threshold resolver.

The previous design pinned thresholds to static probabilities per mode
(e.g. ``hunter: 0.35``). The new design computes a target win rate
from live context and maps it through the model's calibration table to
a probability cut-point. These tests lock the resolution math, the
calibration-table lookup, and the deployment-pressure relief behaviour
that is the central operator dial.
"""

from __future__ import annotations

from models.meta_label.calibration import CalibrationBin, CalibrationTable
from models.meta_label.thresholds import (
    DynamicThresholdConfig,
    ThresholdContext,
    resolve_threshold,
    threshold_for,
)


# ── calibration table parsing & queries ────────────────────────────────


def _baseline_table() -> CalibrationTable:
    return CalibrationTable.from_bins([
        CalibrationBin(predicted=0.294, observed=0.032, n=94),
        CalibrationBin(predicted=0.362, observed=0.267, n=1226),
        CalibrationBin(predicted=0.443, observed=0.450, n=1943),
        CalibrationBin(predicted=0.520, observed=0.732, n=310),
        CalibrationBin(predicted=0.625, observed=1.000, n=106),
    ])


def test_calibration_table_lowest_threshold_for_target() -> None:
    table = _baseline_table()
    # 0.30 win-rate target → first bin clearing it is observed=0.450 → predicted=0.443.
    assert table.lowest_threshold_for(0.30) == 0.443
    # 0.50 target → first bin is observed=0.732 → predicted=0.520.
    assert table.lowest_threshold_for(0.50) == 0.520
    # 0.99 target → only bin >= 0.99 is observed=1.0 → predicted=0.625.
    assert table.lowest_threshold_for(0.99) == 0.625
    # Above the best bin → returns 1.0 sentinel (caller should clamp first).
    assert table.lowest_threshold_for(1.01) == 1.0
    # Below every bin's observed → returns the lowest predicted (most permissive).
    assert table.lowest_threshold_for(-0.5) == 0.294


def test_calibration_table_best_observed_and_base_rate() -> None:
    table = _baseline_table()
    assert table.best_observed == 1.0
    # Sample-size-weighted observed mean ~ 0.418 base rate from validation.
    assert abs(table.base_rate_estimate - 0.418) < 0.01


def test_calibration_table_from_dict_roundtrip() -> None:
    raw = [
        {"predicted": 0.25, "observed": 0.10, "n": 50},
        {"predicted": 0.50, "observed": 0.55, "n": 100},
    ]
    table = CalibrationTable.from_dict(raw)
    assert table is not None
    assert table.lowest_threshold_for(0.50) == 0.50


def test_calibration_table_from_dict_returns_none_on_empty_or_bad() -> None:
    assert CalibrationTable.from_dict(None) is None
    assert CalibrationTable.from_dict([]) is None
    assert CalibrationTable.from_dict([{"predicted": "bad"}]) is None


# ── dynamic resolver: base anchor ──────────────────────────────────────


def test_resolver_neutral_context_uses_base_anchor() -> None:
    cfg = DynamicThresholdConfig(base_anchor=0.42)
    res = resolve_threshold(cfg, context=ThresholdContext(), calibration=_baseline_table())
    # target = 0.42 → first bin clearing 0.42 is observed=0.450 (predicted=0.443).
    assert res.target_win_rate == 0.42
    assert res.threshold == 0.443
    assert res.calibration_used is True


# ── dynamic resolver: mode offsets ─────────────────────────────────────


def test_resolver_mode_offsets_shape_target() -> None:
    cfg = DynamicThresholdConfig(
        base_anchor=0.42,
        by_mode_offset={"hunter": -0.05, "defender": 0.10},
    )
    table = _baseline_table()
    hunter = resolve_threshold(
        cfg, context=ThresholdContext(mode="hunter"), calibration=table,
    )
    defender = resolve_threshold(
        cfg, context=ThresholdContext(mode="defender"), calibration=table,
    )
    assert hunter.target_win_rate == 0.37
    assert defender.target_win_rate == 0.52
    # hunter target 0.37 → first bin observed=0.450 → predicted 0.443.
    assert hunter.threshold == 0.443
    # defender 0.52 → first bin observed=0.732 → predicted 0.520.
    assert defender.threshold == 0.520


# ── dynamic resolver: regime + vol caution ──────────────────────────────


def test_resolver_risk_off_regime_lifts_target() -> None:
    cfg = DynamicThresholdConfig(base_anchor=0.30, regime_caution_weight=0.30)
    table = _baseline_table()
    full_risk_off = resolve_threshold(
        cfg, context=ThresholdContext(market_state_score=0.0), calibration=table,
    )
    # target = 0.30 + 0.30 * 1 = 0.60 → first bin observed=0.732 → 0.520.
    assert full_risk_off.target_win_rate == 0.60
    assert full_risk_off.threshold == 0.520


def test_resolver_high_vol_lifts_target() -> None:
    cfg = DynamicThresholdConfig(base_anchor=0.40, vol_caution_weight=0.10)
    table = _baseline_table()
    high_vol = resolve_threshold(
        cfg, context=ThresholdContext(market_volatility_scalar=3.0), calibration=table,
    )
    # target = 0.40 + 0.10 * 2 = 0.60 → predicted 0.520.
    assert abs(high_vol.target_win_rate - 0.60) < 1e-9
    assert high_vol.threshold == 0.520


# ── deployment pressure relief — the central operator dial ─────────────


def test_deployment_pressure_lowers_target_continuously() -> None:
    cfg = DynamicThresholdConfig(base_anchor=0.50, deployment_relief_weight=0.20)
    table = _baseline_table()
    fully_deployed = resolve_threshold(
        cfg, context=ThresholdContext(deployment_pressure=0.0), calibration=table,
    )
    stranded = resolve_threshold(
        cfg, context=ThresholdContext(deployment_pressure=1.0), calibration=table,
    )
    # No pressure → target = 0.50.
    assert fully_deployed.target_win_rate == 0.50
    # Full pressure → target = 0.50 - 0.20 = 0.30.
    assert stranded.target_win_rate == 0.30
    # Stranded threshold is lower (more permissive).
    assert stranded.threshold < fully_deployed.threshold


def test_deployment_pressure_relief_does_not_breach_floor() -> None:
    """target_floor clamps the relief — never accept worse than the floor."""
    cfg = DynamicThresholdConfig(
        base_anchor=0.30,
        deployment_relief_weight=1.0,  # huge relief
        target_floor=0.25,
    )
    table = _baseline_table()
    res = resolve_threshold(
        cfg, context=ThresholdContext(deployment_pressure=1.0), calibration=table,
    )
    # Pre-clamp target = 0.30 - 1.0 = -0.70.
    assert res.target_pre_clamp == -0.70
    # Floor clamps to whichever is stricter: configured 0.25 vs
    # calibration-derived 0.7 * base_rate ≈ 0.7 * 0.418 ≈ 0.293.
    assert res.target_win_rate >= 0.25


# ── ceiling clamp via calibration evidence ─────────────────────────────


def test_target_ceiling_clamped_to_best_observed() -> None:
    """Never demand a target above the model's best calibrated bin."""
    cfg = DynamicThresholdConfig(base_anchor=2.0, target_ceiling=2.0)
    table = _baseline_table()
    res = resolve_threshold(cfg, context=ThresholdContext(), calibration=table)
    # best_observed is 1.0; target gets clamped there → threshold 0.625.
    assert res.target_win_rate == 1.0
    assert res.threshold == 0.625


# ── mode-regime override replaces sum ──────────────────────────────────


def test_mode_regime_combined_override_replaces_separate_offsets() -> None:
    cfg = DynamicThresholdConfig(
        base_anchor=0.40,
        by_mode_offset={"hunter": -0.05},
        by_regime_offset={"crash": 0.15},
        by_mode_regime_offset={"hunter.crash": 0.08},
    )
    table = _baseline_table()
    # mode+regime present → uses 0.08 override; ignores -0.05 and +0.15.
    res = resolve_threshold(
        cfg,
        context=ThresholdContext(mode="hunter", regime="crash"),
        calibration=table,
    )
    assert abs(res.target_win_rate - 0.48) < 1e-9


# ── backward-compat: threshold_for facade ──────────────────────────────


def test_threshold_for_facade_returns_scalar() -> None:
    cfg = DynamicThresholdConfig(base_anchor=0.42)
    thr = threshold_for(cfg, mode="hunter", calibration=_baseline_table())
    assert isinstance(thr, float)
    assert 0.0 < thr <= 1.0
