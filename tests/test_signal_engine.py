from decimal import Decimal

from signals.engine import RawSignal, SignalEngine


def test_signal_engine_uses_metadata_close_for_quantity():
    engine = SignalEngine({"default_position_pct": 0.1, "quantity_decimals": 6})
    raw = RawSignal(
        strategy="x",
        symbol="SPY",
        side="buy",
        confidence=0.7,
        broker="ibkr",
        asset_class="equity",
        metadata={"close": 100.0},
    )
    sig = engine.process(raw, portfolio_value=Decimal("10000"))
    assert sig is not None
    assert sig.suggested_price == Decimal("100.0")
    assert sig.suggested_quantity == Decimal("10.000000")


def test_signal_engine_vetoes_very_negative_news():
    engine = SignalEngine({"news_veto_threshold": -0.5})
    raw = RawSignal(
        strategy="x",
        symbol="SPY",
        side="buy",
        confidence=0.7,
        broker="ibkr",
        asset_class="equity",
        metadata={"close": 100.0},
    )
    sig = engine.process(raw, portfolio_value=Decimal("10000"), news_score=-0.9)
    assert sig is None

