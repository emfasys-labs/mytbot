"""
tests/test_adaptive_sizing_phase3.py
=====================================
Phase 3 — vol-targeted position sizing.

These tests pin the sizer's behaviour. The point of vol-targeting is
that every trade risks the same dollar amount in a 1-ATR adverse move,
regardless of symbol or asset class.
"""

from __future__ import annotations

from decimal import Decimal

from system.adaptive_sizing import SizingInputs, compute_position_size


# ── Vol targeting flattens dollar risk per ATR ──────────────────────────


def test_low_vol_symbol_gets_larger_position() -> None:
    """A 0.5% ATR symbol should get a bigger position than a 5% ATR symbol."""
    nav = Decimal("100000")
    low = compute_position_size(
        SizingInputs(nav=nav, last_price=Decimal("100"), atr_pct=0.005, mode="hunter")
    )
    high = compute_position_size(
        SizingInputs(nav=nav, last_price=Decimal("100"), atr_pct=0.05, mode="hunter")
    )
    assert low.notional > high.notional
    assert low.path == "vol_targeted"


def test_dollar_risk_per_atr_is_approximately_constant() -> None:
    """``notional × atr_pct`` should be ≈ ``nav × risk_per_trade`` regardless of symbol."""
    nav = Decimal("100000")
    # risk_budget for hunter mode is 0.5% by default = $500 risk per trade
    expected_risk = Decimal("500")
    # Mid-range ATRs where neither the floor nor the ceiling clips us.
    # At hunter 0.5% risk budget: 0.02 → 25% (under 30% cap), 0.05 → 10%,
    # 0.10 → 5%, all uncapped.
    for atr in (0.02, 0.05, 0.10):
        out = compute_position_size(
            SizingInputs(nav=nav, last_price=Decimal("100"), atr_pct=atr, mode="hunter")
        )
        dollar_risk = out.notional * Decimal(str(atr))
        # Within 10% of target (allows for safety clamps at the extremes)
        assert abs(dollar_risk - expected_risk) / expected_risk < Decimal("0.10")


# ── Mode bias on risk budget ───────────────────────────────────────────


def test_defender_sizes_smaller_than_hunter() -> None:
    """Same vol, defender mode → smaller position."""
    nav = Decimal("100000")
    atr = 0.02
    h = compute_position_size(SizingInputs(nav=nav, last_price=Decimal("100"), atr_pct=atr, mode="hunter"))
    t = compute_position_size(SizingInputs(nav=nav, last_price=Decimal("100"), atr_pct=atr, mode="trader"))
    d = compute_position_size(SizingInputs(nav=nav, last_price=Decimal("100"), atr_pct=atr, mode="defender"))
    assert d.notional < t.notional < h.notional


# ── Confidence scaling ──────────────────────────────────────────────────


def test_low_confidence_shrinks_position() -> None:
    """Half-conviction signal → half-sized position."""
    nav = Decimal("100000")
    full = compute_position_size(
        SizingInputs(nav=nav, last_price=Decimal("100"), atr_pct=0.02, mode="hunter", confidence=1.0)
    )
    half = compute_position_size(
        SizingInputs(nav=nav, last_price=Decimal("100"), atr_pct=0.02, mode="hunter", confidence=0.5)
    )
    assert half.notional == (full.notional / 2).quantize(Decimal("0.01"))


# ── Safety rails ────────────────────────────────────────────────────────


def test_max_notional_cap_holds_even_for_tiny_vol() -> None:
    """A 0.01% ATR symbol shouldn't get sized to 50% NAV."""
    nav = Decimal("100000")
    out = compute_position_size(
        SizingInputs(nav=nav, last_price=Decimal("100"), atr_pct=0.0001, mode="hunter")
    )
    # Max 30% of NAV = $30k
    assert out.notional <= Decimal("30000")


def test_min_notional_floor_holds_for_huge_vol() -> None:
    """A 50% ATR symbol shouldn't drop below the 0.5% floor."""
    nav = Decimal("100000")
    out = compute_position_size(
        SizingInputs(nav=nav, last_price=Decimal("100"), atr_pct=0.5, mode="defender")
    )
    # Min 0.5% of NAV = $500
    assert out.notional >= Decimal("500")


# ── Fallback paths ──────────────────────────────────────────────────────


def test_missing_atr_uses_static_fallback() -> None:
    """No atr_pct → fall back to fixed-pct sizing (legacy behaviour)."""
    nav = Decimal("100000")
    out = compute_position_size(
        SizingInputs(nav=nav, last_price=Decimal("100"), atr_pct=None, fallback_position_pct=0.05)
    )
    assert out.path == "fallback_static_pct"
    # 5% of $100k = $5k
    assert out.notional == Decimal("5000.00")


def test_zero_atr_uses_static_fallback() -> None:
    """ATR=0 (broken data) → static fallback, not divide-by-zero."""
    nav = Decimal("100000")
    out = compute_position_size(
        SizingInputs(nav=nav, last_price=Decimal("100"), atr_pct=0.0, fallback_position_pct=0.05)
    )
    assert out.path == "fallback_static_pct"


def test_no_nav_returns_zero() -> None:
    """Zero NAV → zero size, no crash."""
    out = compute_position_size(SizingInputs(nav=Decimal("0"), last_price=Decimal("100"), atr_pct=0.02))
    assert out.notional == Decimal("0")
    assert out.path == "no_nav"


def test_missing_last_price_keeps_notional() -> None:
    """No price → quantity=0 but notional surfaced for callers to inspect."""
    nav = Decimal("100000")
    out = compute_position_size(SizingInputs(nav=nav, last_price=None, atr_pct=0.02, mode="hunter"))
    assert out.quantity == Decimal("0")
    assert out.notional > Decimal("0")  # still a real dollar figure
