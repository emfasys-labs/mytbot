from __future__ import annotations

from decimal import Decimal

from signals.engine import RawSignal, SignalEngine


def test_volatility_sizing_scales_down_when_high_atr() -> None:
    cfg = {
        "default_position_pct": 0.10,
        "min_quantity": 0.0001,
        "quantity_decimals": 4,
        "volatility_sizing": {
            "enabled": True,
            "target_atr_pct": 0.02,
            "min_scale": 0.25,
            "max_scale": 2.0,
        },
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
    # Base ~100 shares (10% * 100k / 100); scale 0.02/0.08 = 0.25 (min_scale floor)
    assert out.suggested_quantity == Decimal("25.0000")


def test_volatility_sizing_disabled_uses_base_only() -> None:
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
    assert out.suggested_quantity == Decimal("100.0000")
