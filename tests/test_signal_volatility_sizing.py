"""
tests/test_signal_volatility_sizing.py
=======================================

Legacy vol-sizing tests, updated for Phase 3.

Before Phase 3, ``volatility_sizing.enabled = True`` triggered a
``base × (target_atr/actual_atr)`` scaling on top of the static
``default_position_pct``. The arithmetic was clamped by ``min_scale`` /
``max_scale``.

Phase 3 replaces that with a risk-budget-driven sizer:
``notional = NAV × risk_per_trade / atr_pct``. The old YAML knobs
(``target_atr_pct``, ``min_scale``, ``max_scale``) are ignored when
adaptive sizing is enabled. The contract is preserved (higher vol →
smaller size) but the exact arithmetic differs.

These tests now exercise both:
  * the new adaptive path (high vol → smaller size, contract test),
  * the operator opt-out (``volatility_sizing.enabled = False`` →
    pure legacy ``nav × default_position_pct``).
"""

from __future__ import annotations

from decimal import Decimal

from signals.engine import RawSignal, SignalEngine


def test_volatility_sizing_scales_down_when_high_atr() -> None:
    """Phase 3 contract: high-ATR symbol gets a smaller position than
    low-ATR. Exact arithmetic is risk_per_trade / atr_pct, not the
    legacy target/actual scaling — but the *direction* is preserved."""
    cfg = {
        "default_position_pct": 0.10,
        "min_quantity": 0.0001,
        "quantity_decimals": 4,
        # Adaptive is the default; we still set it explicitly for clarity.
        "volatility_sizing": {"enabled": True},
    }
    eng = SignalEngine(cfg)
    low_vol = eng.process(
        RawSignal(
            strategy="momentum_breakout", symbol="LOW", side="buy",
            confidence=0.8, broker="ibkr", asset_class="equity",
            metadata={"close": 100.0, "atr_pct": 0.01},
        ),
        portfolio_value=Decimal("100000"), news_score=None,
    )
    high_vol = eng.process(
        RawSignal(
            strategy="momentum_breakout", symbol="HI", side="buy",
            confidence=0.8, broker="ibkr", asset_class="equity",
            metadata={"close": 100.0, "atr_pct": 0.08},
        ),
        portfolio_value=Decimal("100000"), news_score=None,
    )
    assert low_vol is not None and high_vol is not None
    # Contract: smaller position on higher-vol symbol.
    assert high_vol.suggested_quantity < low_vol.suggested_quantity
    # Concretely: risk 0.5% / atr 0.08 = 6.25% NAV → $6,250 → 62.5 shares.
    # × confidence 0.8 → 50 shares.
    assert high_vol.suggested_quantity == Decimal("50.0000")


def test_volatility_sizing_disabled_uses_base_only() -> None:
    """Operator opt-out: ``enabled=False`` returns to pure legacy sizing
    (``NAV × default_position_pct`` with no vol or confidence scaling)."""
    cfg = {
        "default_position_pct": 0.10,
        "min_quantity": 0.0001,
        "quantity_decimals": 4,
        "volatility_sizing": {"enabled": False},
    }
    eng = SignalEngine(cfg)
    raw = RawSignal(
        strategy="momentum_breakout",
        symbol="SPY",
        side="buy",
        confidence=0.8,
        broker="ibkr",
        asset_class="equity",
        metadata={"close": 100.0, "atr_pct": 0.08},
    )
    out = eng.process(raw, portfolio_value=Decimal("100000"), news_score=None)
    assert out is not None
    # Legacy contract: 10% × $100k / $100 = 100 shares. No confidence, no vol scaling.
    assert out.suggested_quantity == Decimal("100.0000")
