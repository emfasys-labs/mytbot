"""
tests/test_adaptive_regime_weights.py
======================================
Strategy × market opportunity multipliers — locking the *qualitative*
behaviour (directional alignments, safety bounds, fall-through) of the
dynamic formula in ``system/adaptive_regime_weights.py``.

After D140 v2 the multiplier is COMPUTED LIVE from continuous market
features, not looked up from a fixed table. The label-based
``strategy_regime_multiplier`` API is preserved as a backwards-compat
wrapper that synthesises features from the discrete regime label.
These tests pin the wrapper's qualitative output so existing callers
(``opportunity_engine``) keep working.

The directional alignments (momentum thrives in trends, mean-reversion
in ranges, etc.) come from each strategy's affinity row in code —
those are categorical design statements, not tuning constants. The
safety bounds come from ``config/strategies.yaml::regime_weights.bounds``.
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
    assert stress >= Decimal("1.0")
    assert stress >= calm


def test_pairs_trading_opposite_in_trends_vs_ranges() -> None:
    trend = strategy_regime_multiplier("pairs_trading", "trend_up")
    range_ = strategy_regime_multiplier("pairs_trading", "range")
    assert range_ > Decimal("1.0") > trend


# ── Safety bounds (YAML-driven) ─────────────────────────────────────────


def test_multiplier_stays_inside_yaml_bounds() -> None:
    """Every strategy × regime combo must produce a multiplier inside
    the YAML safety band — the formula plus the post-clamp guarantee
    it. Default YAML has bounds [0.5, 1.5]."""
    strategies = [
        "momentum_breakout", "volume_flow", "mean_reversion",
        "volatility_regime", "event_driven_news", "pairs_trading",
        "regime_rotation",
    ]
    regimes = [
        "trend_up", "trend_down", "range", "volatile",
        "crash", "panic", "risk_on", "risk_off", "mixed",
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
