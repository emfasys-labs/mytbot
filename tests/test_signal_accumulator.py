"""Unit tests for signals.accumulator (time decay, alignment, stale reset)."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from signals.accumulator import InputSignal, NetSignal, SignalAccumulator, raw_signal_to_input_signal
from signals.engine import RawSignal


def _ts() -> datetime:
    return datetime(2026, 4, 12, 12, 0, tzinfo=timezone.utc)


def test_input_signal_validation():
    with pytest.raises(ValueError):
        InputSignal(
            symbol="SPY",
            source_type="quant",
            source_name="m",
            direction=2,
            strength=Decimal("0.5"),
            confidence=Decimal("0.8"),
            horizon="short",
            timestamp=_ts(),
        )


def test_decay_reduces_score():
    acc = SignalAccumulator()
    t0 = _ts()
    acc.update(
        InputSignal(
            symbol="SPY",
            source_type="quant",
            source_name="mom",
            direction=1,
            strength=Decimal("0.8"),
            confidence=Decimal("0.9"),
            horizon="short",
            timestamp=t0,
        ),
        now=t0,
    )
    t1 = t0 + timedelta(minutes=90)
    st = acc.get_state("SPY")
    assert st is not None
    acc.apply_time_decay(st, t1)
    assert st.short_score < Decimal("0.5")


def test_alignment_bonus_stronger_net():
    acc = SignalAccumulator()
    t0 = _ts()
    for horizon, src in (
        ("short", "q1"),
        ("medium", "q2"),
        ("long", "q3"),
    ):
        acc.update(
            InputSignal(
                symbol="SPY",
                source_type="quant",
                source_name=src,
                direction=1,
                strength=Decimal("0.5"),
                confidence=Decimal("0.8"),
                horizon=horizon,
                timestamp=t0,
            ),
            now=t0,
        )
    net = acc.compute_net_for_symbol("SPY", t0)
    assert net is not None
    assert net.score > Decimal("0")
    assert "alignment_bonus" in net.components
    assert net.components["alignment_bonus"] > Decimal("0")


def test_conflict_penalty_when_horizons_diverge():
    acc = SignalAccumulator()
    t0 = _ts()
    acc.update(
        InputSignal(
            symbol="QQQ",
            source_type="quant",
            source_name="a",
            direction=1,
            strength=Decimal("0.9"),
            confidence=Decimal("0.9"),
            horizon="short",
            timestamp=t0,
        ),
        now=t0,
    )
    acc.update(
        InputSignal(
            symbol="QQQ",
            source_type="quant",
            source_name="b",
            direction=-1,
            strength=Decimal("0.9"),
            confidence=Decimal("0.9"),
            horizon="long",
            timestamp=t0,
        ),
        now=t0,
    )
    net = acc.compute_net_for_symbol("QQQ", t0)
    assert net is not None
    assert net.components["conflict_penalty"] > Decimal("0")


def test_stale_reset():
    acc = SignalAccumulator(stale_reset_minutes=60)
    t0 = _ts()
    acc.update(
        InputSignal(
            symbol="X",
            source_type="news",
            source_name="n",
            direction=1,
            strength=Decimal("0.5"),
            confidence=Decimal("0.7"),
            horizon="medium",
            timestamp=t0,
        ),
        now=t0,
    )
    t_idle = t0 + timedelta(minutes=120)
    st = acc.get_state("X")
    assert st is not None
    acc.reset_if_stale(st, t_idle)
    assert st.short_score == Decimal("0")
    assert st.medium_score == Decimal("0")


def test_raw_signal_to_input_signal_hold_returns_none():
    raw = RawSignal("s", "SPY", "hold", 0.5, "ibkr", "equity", {})
    assert raw_signal_to_input_signal(raw, timestamp=_ts()) is None


def test_raw_signal_to_input_signal_buy():
    raw = RawSignal("s", "spy", "buy", 0.6, "ibkr", "equity", {"signal_horizon": "medium"})
    inp = raw_signal_to_input_signal(raw, timestamp=_ts())
    assert inp is not None
    assert inp.symbol == "SPY"
    assert inp.direction == 1
    assert inp.horizon == "medium"


def test_feed_ai_pipeline_result_smoke():
    from ai.pipeline import AIPipelineResult

    acc = SignalAccumulator()
    r = AIPipelineResult(
        news_scores={"SPY": 0.4},
        macro_regime="easing",
        macro_confidence=0.7,
        macro_payload={},
        news_details={
            "SPY": {"confidence": 0.8, "decay_hours": 12},
        },
        anomalies=[],
    )
    acc.feed_ai_pipeline_result(r, ["SPY"], now=_ts())
    net = acc.compute_net_for_symbol("SPY", _ts())
    assert net is not None
    assert isinstance(net, NetSignal)


def test_dashboard_snapshot_ranking_and_json_safe():
    import json

    acc = SignalAccumulator()
    t0 = _ts()
    acc.update(
        InputSignal(
            symbol="WEAK",
            source_type="quant",
            source_name="m",
            direction=-1,
            strength=Decimal("0.3"),
            confidence=Decimal("0.5"),
            horizon="short",
            timestamp=t0,
        ),
        now=t0,
    )
    acc.update(
        InputSignal(
            symbol="STRONG",
            source_type="quant",
            source_name="m",
            direction=1,
            strength=Decimal("0.95"),
            confidence=Decimal("0.95"),
            horizon="short",
            timestamp=t0,
        ),
        now=t0,
    )
    snap = acc.dashboard_snapshot(top_n=5, now=t0)
    json.dumps(snap)  # JSON-safe
    assert snap["updated_at"]
    assert {e["symbol"] for e in snap["bullish_top"]} >= {"STRONG"}
    assert {e["symbol"] for e in snap["bearish_top"]} >= {"WEAK"}
    # STRONG should rank first by magnitude
    assert snap["top_by_magnitude"][0]["symbol"] == "STRONG"
    for e in snap["top_by_magnitude"]:
        assert "components" in e
        assert "source_types_seen" in e
