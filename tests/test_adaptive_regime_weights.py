"""
tests/test_adaptive_regime_weights.py
======================================
Phase 4 — strategy×regime opportunity-score multipliers.

These tests pin down:
  * the directional alignments (momentum fades in ranges; mean-reversion
    fades in trends; volatility_regime amplifies in stress)
  * the safety bounds (no multiplier below 0.5 or above 1.5)
  * fall-through behaviour (unknown strategy or regime → 1.0)

The point is that a regime misclassification can hurt PnL but never
silence a strategy entirely.
"""

from __future__ import annotations

from decimal import Decimal

from system.adaptive_regime_weights import strategy_regime_multiplier


# ── Directional alignments ──────────────────────────────────────────────


def test_momentum_amplified_in_trends_faded_in_ranges() -> None:
    trend = strategy_regime_multiplier("momentum_breakout", "trend_up")
    range_ = strategy_regime_multiplier("momentum_breakout", "range")
    assert trend > Decimal("1.0")
    assert range_ < Decimal("1.0")


def test_mean_reversion_opposite_of_momentum() -> None:
    mom_trend = strategy_regime_multiplier("momentum_breakout", "trend_up")
    mr_trend = strategy_regime_multiplier("mean_reversion", "trend_up")
    mom_range = strategy_regime_multiplier("momentum_breakout", "range")
    mr_range = strategy_regime_multiplier("mean_reversion", "range")
    # Both go in opposite directions in each regime.
    assert mom_trend > Decimal("1.0") > mr_trend
    assert mr_range > Decimal("1.0") > mom_range


def test_volatility_regime_amplified_in_volatile_and_crash() -> None:
    quiet = strategy_regime_multiplier("volatility_regime", "trend_up")
    vol = strategy_regime_multiplier("volatility_regime", "volatile")
    crash = strategy_regime_multiplier("volatility_regime", "crash")
    assert vol > Decimal("1.0")
    assert crash > Decimal("1.0")
    assert vol > quiet


def test_event_driven_amplified_in_panic_and_risk_off() -> None:
    calm = strategy_regime_multiplier("event_driven_news", "trend_up")
    stress = strategy_regime_multiplier("event_driven_news", "panic")
    risk_off = strategy_regime_multiplier("event_driven_news", "risk_off")
    assert stress >= Decimal("1.0")
    assert risk_off >= Decimal("1.0")
    assert stress >= calm


def test_pairs_trading_opposite_in_trends_vs_ranges() -> None:
    trend = strategy_regime_multiplier("pairs_trading", "trend_up")
    range_ = strategy_regime_multiplier("pairs_trading", "range")
    assert range_ > Decimal("1.0") > trend


# ── Safety bounds ───────────────────────────────────────────────────────


def test_multiplier_never_silences_a_strategy() -> None:
    """Every strategy×regime combo must produce ≥ 0.5 multiplier."""
    strategies = [
        "momentum_breakout", "volume_flow", "mean_reversion",
        "volatility_regime", "event_driven_news", "pairs_trading",
        "regime_rotation",
    ]
    regimes = [
        "trend_up", "trend_down", "range", "volatile",
        "crash", "panic", "risk_on", "risk_off",
    ]
    for s in strategies:
        for r in regimes:
            mult = strategy_regime_multiplier(s, r)
            assert mult >= Decimal("0.5"), f"{s} × {r} silenced: {mult}"
            assert mult <= Decimal("1.5"), f"{s} × {r} oversized: {mult}"


# ── Fall-through behaviour ──────────────────────────────────────────────


def test_unknown_strategy_falls_through_to_neutral() -> None:
    assert strategy_regime_multiplier("totally_made_up", "trend_up") == Decimal("1.0")


def test_unknown_regime_falls_through_to_neutral() -> None:
    assert strategy_regime_multiplier("momentum_breakout", "supercrash") == Decimal("1.0")


def test_empty_strategy_or_regime_is_neutral() -> None:
    assert strategy_regime_multiplier("", "trend_up") == Decimal("1.0")
    assert strategy_regime_multiplier("momentum_breakout", "") == Decimal("1.0")


def test_case_insensitive_lookup() -> None:
    upper = strategy_regime_multiplier("MOMENTUM_BREAKOUT", "TREND_UP")
    lower = strategy_regime_multiplier("momentum_breakout", "trend_up")
    assert upper == lower
