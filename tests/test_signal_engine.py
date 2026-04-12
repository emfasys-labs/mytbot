from datetime import datetime, timezone
from decimal import Decimal

from signals.accumulator import SignalAccumulator
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


def test_signal_engine_accumulator_metadata_and_news_score():
    acc = SignalAccumulator()
    now = datetime.now(timezone.utc)
    cfg = {
        "default_position_pct": 0.1,
        "quantity_decimals": 6,
        "news_confidence_weight": 0.15,
        "news_veto_threshold": -0.95,
        "accumulator_dual_ai_veto": False,
    }
    engine = SignalEngine(cfg, accumulator=acc)
    from ai.pipeline import AIPipelineResult

    acc.feed_ai_pipeline_result(
        AIPipelineResult(
            news_scores={"SPY": 0.3},
            macro_regime="neutral",
            macro_confidence=0.4,
            macro_payload={},
            news_details={"SPY": {"confidence": 0.8, "decay_hours": 24}},
            anomalies=[],
        ),
        ["SPY"],
        now=now,
    )
    raw = RawSignal(
        strategy="momentum_breakout",
        symbol="SPY",
        side="buy",
        confidence=0.6,
        broker="ibkr",
        asset_class="equity",
        metadata={"close": 100.0},
    )
    sig = engine.process(raw, portfolio_value=Decimal("10000"), news_score=0.3)
    assert sig is not None
    assert "accumulator_score" in sig.metadata
    assert "ai_news_score" in sig.metadata
    assert sig.news_score is not None

