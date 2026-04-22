"""D031 closure — SignalEngine must respect coordinator-supplied sizing.

Regression tests for the bug that caused the April 2026 "Sizing boundary guard
rejected signal" wave: SignalEngine.process() was ignoring
metadata["target_notional"] / metadata["risk_notional_override"] on the
directional path and rebuilding quantity from nav * default_position_pct,
then applying volatility sizing on top, which double-scaled low-ATR symbols
to ~2x the coordinator's intent.
"""

from __future__ import annotations

from decimal import Decimal

from signals.engine import RawSignal, SignalEngine


def _cfg(**overrides: object) -> dict:
    base = {
        "default_position_pct": 0.05,
        "min_quantity": 0.0001,
        "quantity_decimals": 4,
        "volatility_sizing": {
            "enabled": True,
            "target_atr_pct": 0.02,
            "min_scale": 0.25,
            "max_scale": 2.0,
        },
    }
    base.update(overrides)
    return base


def test_target_notional_overrides_nav_fallback_directional() -> None:
    """target_notional from coordinator must be used directly, not overwritten."""
    eng = SignalEngine(_cfg())
    raw = RawSignal(
        strategy="mean_reversion",
        symbol="ES",
        side="buy",
        confidence=0.7,
        broker="alpaca",
        asset_class="equity",
        metadata={
            "close": 500.0,
            "atr_pct": 0.01,  # low vol — would normally scale up 2x
            "target_notional": "4997.00",
        },
    )
    out = eng.process(raw, portfolio_value=Decimal("99940"), news_score=None)
    assert out is not None
    # 4997 / 500 = 9.994 → quantize(1e-4) = 9.994
    assert out.suggested_quantity == Decimal("9.9940")
    # Resolved notional back to target — no double scaling.
    assert out.metadata["signal_engine_sizing_path"] == "target_notional"
    resolved = Decimal(out.metadata["signal_engine_resolved_notional"])
    assert abs(resolved - Decimal("4997.00")) < Decimal("0.01")


def test_risk_notional_override_beats_target_notional() -> None:
    """risk_notional_override must take priority over target_notional."""
    eng = SignalEngine(_cfg())
    raw = RawSignal(
        strategy="momentum",
        symbol="BIV",
        side="buy",
        confidence=0.6,
        broker="alpaca",
        asset_class="etf",
        metadata={
            "close": 80.0,
            "atr_pct": 0.005,  # ultra-low vol; would double under old path
            "target_notional": "10000.00",
            "risk_notional_override": "4000.00",
        },
    )
    out = eng.process(raw, portfolio_value=Decimal("100000"), news_score=None)
    assert out is not None
    assert out.metadata["signal_engine_sizing_path"] == "risk_notional_override"
    # 4000 / 80 = 50
    assert out.suggested_quantity == Decimal("50.0000")


def test_coordinator_sizing_skips_volatility_double_scaling() -> None:
    """The coordinator's intent must NOT be multiplied by volatility scaling.

    Regression: pre-fix, low atr_pct=0.01 caused a 2x scale that pushed the
    final order to 2x intended, tripping the execution boundary guard.
    """
    eng = SignalEngine(_cfg())
    raw = RawSignal(
        strategy="mean_reversion",
        symbol="EURUSD",
        side="buy",
        confidence=0.7,
        broker="ibkr",
        asset_class="fx",
        metadata={
            "close": 1.10,
            "atr_pct": 0.002,  # would otherwise scale 10x → clamped 2x
            "target_notional": "5000.00",
        },
    )
    out = eng.process(raw, portfolio_value=Decimal("100000"), news_score=None)
    assert out is not None
    resolved = Decimal(out.metadata["signal_engine_resolved_notional"])
    # Intent was 5000. After fix, actual must be within rounding (no 2x blow-up).
    assert resolved <= Decimal("5001")
    assert resolved >= Decimal("4999")


def test_legacy_nav_fallback_still_applies_volatility_scaling() -> None:
    """When coordinator does not supply sizing, legacy path still works."""
    eng = SignalEngine(_cfg())
    raw = RawSignal(
        strategy="momentum_breakout",
        symbol="SPY",
        side="buy",
        confidence=0.8,
        broker="ibkr",
        asset_class="equity",
        metadata={"close": 100.0, "atr_pct": 0.08},  # no target_notional
    )
    out = eng.process(raw, portfolio_value=Decimal("100000"), news_score=None)
    assert out is not None
    # nav 100k * 0.05 / 100 = 50 base; scale 0.02/0.08 = 0.25 → 12.5
    assert out.metadata["signal_engine_sizing_path"] == "nav_fallback"
    assert out.suggested_quantity == Decimal("12.5000")


def test_zero_or_negative_target_notional_falls_back_to_nav() -> None:
    eng = SignalEngine(_cfg())
    raw = RawSignal(
        strategy="momentum",
        symbol="QQQ",
        side="buy",
        confidence=0.7,
        broker="alpaca",
        asset_class="equity",
        metadata={"close": 400.0, "atr_pct": 0.02, "target_notional": "0"},
    )
    out = eng.process(raw, portfolio_value=Decimal("100000"), news_score=None)
    assert out is not None
    assert out.metadata["signal_engine_sizing_path"] == "nav_fallback"
