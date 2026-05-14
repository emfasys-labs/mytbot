"""
tests/test_adaptive_cadence.py
===============================

Lock in the Phase 1 adaptive loop cadence semantics.

The loop's sleep interval is now a function of (mode, signal density,
session window) instead of a static mode→seconds map. These tests pin
down the formula's behaviour across the regimes that matter:

  * Burst signal density → faster cadence
  * Dry signal density → slower cadence
  * Hunter mode → fast end of the band
  * Defender mode → slow end of the band
  * Off-hours → modest slowdown
  * Floor/ceiling clamps prevent ridiculous values
"""

from __future__ import annotations

from datetime import datetime, timezone

from system.adaptive_cadence import CadenceInputs, compute_loop_cadence


def _us_session() -> datetime:
    """Tuesday 15:00 UTC — middle of the US session."""
    return datetime(2026, 5, 14, 15, 0, tzinfo=timezone.utc)


def _overnight() -> datetime:
    """Tuesday 02:00 UTC — Asian/pre-Europe session."""
    return datetime(2026, 5, 14, 2, 0, tzinfo=timezone.utc)


def _weekend() -> datetime:
    """Saturday 15:00 UTC."""
    return datetime(2026, 5, 16, 15, 0, tzinfo=timezone.utc)


# ── Hunter is the default → fastest cadence ─────────────────────────────


def test_hunter_normal_density_normal_session_equals_base() -> None:
    out = compute_loop_cadence(
        CadenceInputs(
            mode="hunter",
            recent_signal_density=5.0,
            base_interval_sec=120.0,
            now=_us_session(),
        )
    )
    # 120 × 1.0 × 1.0 × 1.0 = 120
    assert out == 120.0


def test_hunter_burst_speeds_up() -> None:
    out = compute_loop_cadence(
        CadenceInputs(
            mode="hunter",
            recent_signal_density=15.0,  # > burst threshold (10)
            base_interval_sec=120.0,
            now=_us_session(),
        )
    )
    # 120 × 1.0 × 1.0 × 0.4 = 48
    assert out == 48.0


def test_hunter_dry_slows_down() -> None:
    out = compute_loop_cadence(
        CadenceInputs(
            mode="hunter",
            recent_signal_density=0.0,  # below dry threshold (1.0)
            base_interval_sec=120.0,
            now=_us_session(),
        )
    )
    # 120 × 1.0 × 1.0 × 2.0 = 240
    assert out == 240.0


# ── Defender / trader are slower than hunter ─────────────────────────────


def test_defender_is_much_slower_than_hunter() -> None:
    h = compute_loop_cadence(CadenceInputs(mode="hunter", base_interval_sec=120, now=_us_session()))
    d = compute_loop_cadence(CadenceInputs(mode="defender", base_interval_sec=120, now=_us_session()))
    assert d > h
    assert d == 120 * 2.5  # 300


def test_trader_is_between_hunter_and_defender() -> None:
    h = compute_loop_cadence(CadenceInputs(mode="hunter", base_interval_sec=120, now=_us_session()))
    t = compute_loop_cadence(CadenceInputs(mode="trader", base_interval_sec=120, now=_us_session()))
    d = compute_loop_cadence(CadenceInputs(mode="defender", base_interval_sec=120, now=_us_session()))
    assert h < t < d


# ── Session-window effects ──────────────────────────────────────────────


def test_overnight_session_slows_down() -> None:
    day = compute_loop_cadence(CadenceInputs(mode="hunter", base_interval_sec=120, now=_us_session()))
    night = compute_loop_cadence(CadenceInputs(mode="hunter", base_interval_sec=120, now=_overnight()))
    assert night > day


def test_weekend_is_slowest_session() -> None:
    day = compute_loop_cadence(CadenceInputs(mode="hunter", base_interval_sec=120, now=_us_session()))
    weekend = compute_loop_cadence(CadenceInputs(mode="hunter", base_interval_sec=120, now=_weekend()))
    assert weekend > day


# ── Floor / ceiling clamps ──────────────────────────────────────────────


def test_floor_clamp_prevents_runaway_acceleration() -> None:
    """Even hunter + burst + tiny base shouldn't go below the floor."""
    out = compute_loop_cadence(
        CadenceInputs(
            mode="hunter",
            recent_signal_density=100.0,
            base_interval_sec=10.0,
            now=_us_session(),
        )
    )
    # 10 × 1.0 × 1.0 × 0.4 = 4 → clamped to floor 30
    assert out == 30.0


def test_ceiling_clamp_prevents_runaway_deceleration() -> None:
    """Defender + dry + weekend + big base shouldn't blow past the ceiling."""
    out = compute_loop_cadence(
        CadenceInputs(
            mode="defender",
            recent_signal_density=0.0,
            base_interval_sec=300.0,
            now=_weekend(),
        )
    )
    # 300 × 1.5 × 2.5 × 2.0 = 2250 → clamped to ceiling 600
    assert out == 600.0


# ── Robustness ──────────────────────────────────────────────────────────


def test_unknown_mode_falls_through_to_hunter_speed() -> None:
    out = compute_loop_cadence(
        CadenceInputs(
            mode="something_silly",
            recent_signal_density=5.0,
            base_interval_sec=120.0,
            now=_us_session(),
        )
    )
    assert out == 120.0  # treated as hunter


def test_missing_density_uses_neutral_multiplier() -> None:
    """``None`` density (fresh boot) shouldn't break the calc."""
    out = compute_loop_cadence(
        CadenceInputs(
            mode="hunter",
            recent_signal_density=None,
            base_interval_sec=120.0,
            now=_us_session(),
        )
    )
    assert out == 120.0
