from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pandas as pd

from core.models_runtime import SignalCandidate
from signals.meta_labeler import filter_candidates
from system.cross_asset_demand_graph import CrossAssetDemandGraph
from system.demand_engine import DemandEngine


class _AIResult:
    def __init__(self) -> None:
        self.news_scores = {"SPY": 0.5, "QQQ": 0.3}
        self.macro_regime = "risk_on"
        self.macro_confidence = 0.8


def _df(a: float, b: float) -> pd.DataFrame:
    idx = pd.date_range("2024-01-01", periods=2, freq="D", tz="UTC")
    return pd.DataFrame({"close": [a, b]}, index=idx)


def test_demand_engine_outputs_bounded_signal() -> None:
    eng = DemandEngine({"enabled": True})
    out = eng.compute(
        ai_result=_AIResult(),
        feature_map={"SPY": _df(100, 101), "TLT": _df(100, 99.8)},
    )
    assert -1.0 <= out.score <= 1.0
    assert out.trend in {"rising", "falling", "flat"}
    assert 0.0 <= out.confidence <= 1.0


def test_meta_filter_drops_countertrend_when_demand_strong() -> None:
    c_long = SignalCandidate(
        symbol="SPY",
        asset_class="equity",
        side="long",
        timestamp=datetime.now(timezone.utc),
        raw_signal_strength=Decimal("0.7"),
        adjusted_signal_strength=Decimal("0.7"),
        confidence=Decimal("0.7"),
        strategy_name="momentum_breakout",
        metadata={},
    )
    c_short = SignalCandidate(
        symbol="SPY",
        asset_class="equity",
        side="short",
        timestamp=datetime.now(timezone.utc),
        raw_signal_strength=Decimal("0.72"),
        adjusted_signal_strength=Decimal("0.72"),
        confidence=Decimal("0.72"),
        strategy_name="momentum_breakout",
        metadata={},
    )
    kept, res = filter_candidates(
        [c_long, c_short],
        demand_score=0.8,
        cfg={"enabled": True, "min_confidence": 0.5, "demand_alignment_threshold": 0.45},
    )
    assert len(kept) == 1
    assert kept[0].side == "long"
    assert res.dropped == 1
    assert res.avg_probability > 0


def test_cross_asset_graph_returns_score_and_volatility() -> None:
    graph = CrossAssetDemandGraph(
        {
            "risk_on_anchors": ["SPY"],
            "risk_off_anchors": ["TLT"],
            "graph_edges": [["SPY", "TLT", -1.0]],
            "cross_asset_scale": 30.0,
        }
    )
    out = graph.evaluate({"SPY": _df(100, 101), "TLT": _df(100, 99.7)})
    assert -1.0 <= out.score <= 1.0
    assert out.market_volatility >= 0.0
    assert 0.0 <= out.coverage <= 1.0


def test_meta_filter_mode_calibration_changes_strictness() -> None:
    c = SignalCandidate(
        symbol="SPY",
        asset_class="equity",
        side="long",
        timestamp=datetime.now(timezone.utc),
        raw_signal_strength=Decimal("0.62"),
        adjusted_signal_strength=Decimal("0.62"),
        confidence=Decimal("0.62"),
        strategy_name="momentum_breakout",
        metadata={"volume_z_score": 0.2, "ai_news_score": 0.1},
    )
    cfg = {
        "probability_threshold": 0.54,
        "mode_calibration": {
            "defender": {"probability_threshold": 0.70},
            "hunter": {"probability_threshold": 0.45},
        },
    }
    kept_def, _ = filter_candidates([c], demand_score=0.2, cfg=cfg, mode="defender")
    kept_hnt, _ = filter_candidates([c], demand_score=0.2, cfg=cfg, mode="hunter")
    assert len(kept_def) <= len(kept_hnt)


def test_meta_filter_uses_default_when_mode_missing() -> None:
    c = SignalCandidate(
        symbol="SPY",
        asset_class="equity",
        side="long",
        timestamp=datetime.now(timezone.utc),
        raw_signal_strength=Decimal("0.8"),
        adjusted_signal_strength=Decimal("0.8"),
        confidence=Decimal("0.8"),
        strategy_name="momentum_breakout",
        metadata={},
    )
    cfg = {"probability_threshold": 0.5}
    kept, _ = filter_candidates([c], demand_score=0.0, cfg=cfg, mode="unknown_mode")
    assert len(kept) == 1
