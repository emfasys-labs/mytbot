"""Phase A AI-fusion spine: contract, adapter, combiner, shadow safety."""

from __future__ import annotations

import asyncio

from signals.fusion import (
    ModelSignal,
    build_fusion_inputs_from_metadata,
    fuse,
)
from system.fusion_shadow import fusion_shadow_enabled, log_fusion_shadow_for_signal


def test_model_signal_clamps_and_sanitises() -> None:
    s = ModelSignal(
        model_id="x", model_version="1",
        direction=9.0, expected_return_bps=float("inf"),
        confidence=2.0, horizon="swing", regime_tag="trend_up",
        reliability_prior=-1.0, fallback=False,
    )
    assert s.direction == 1.0
    assert s.confidence == 1.0
    assert s.reliability_prior == 0.0
    assert s.expected_return_bps == 0.0


def test_adapter_reads_live_metadata_readonly() -> None:
    md = {
        "regime_label": "trend_up",
        "forecast_used": True,
        "forecast_expected_return": 0.012,        # +1.2%
        "forecast_confidence_blended": 0.7,
        "accumulator_score": 0.4,
        "accumulator_confidence": 0.6,
        "ai_news_score": -0.3,
        "demand_score": 0.5,
        "demand_alignment": 0.2,
        "regime_strategy_multiplier": 1.3,
        "meta_label_probability": 0.61,
        "volume_z_score": 2.0,
    }
    snapshot = dict(md)
    fi = build_fusion_inputs_from_metadata(
        symbol="SPY", side="long", base_confidence=0.55, metadata=md
    )
    assert md == snapshot, "adapter must not mutate metadata"
    ids = {s.model_id: s for s in fi.signals}
    assert ids["price_forecast"].fallback is False
    assert ids["price_forecast"].expected_return_bps == 120.0  # 0.012 * 1e4
    assert ids["price_forecast"].direction > 0
    assert ids["news_ai"].direction < 0
    assert ids["meta_labeler"].confidence == 0.61
    assert ids["accumulator"].fallback is False
    assert fi.regime_label == "trend_up"


def test_adapter_marks_missing_sources_fallback() -> None:
    fi = build_fusion_inputs_from_metadata(
        symbol="X", side="long", base_confidence=0.5, metadata={}
    )
    assert all(s.fallback for s in fi.signals)
    ev = fuse(fi)
    assert ev.notes == "no_active_evidence"
    assert ev.combined_direction == 0.0
    assert ev.meta_label_probability is None


def test_fuse_combines_and_measures_agreement() -> None:
    md = {
        "regime_label": "trend_up",
        "forecast_used": True,
        "forecast_expected_return": 0.02,
        "forecast_confidence_blended": 0.8,
        "accumulator_score": 0.5,
        "accumulator_confidence": 0.7,
        "ai_news_score": 0.4,            # also positive ⇒ agreement high
        "meta_label_probability": 0.58,
    }
    ev = fuse(build_fusion_inputs_from_metadata(
        symbol="SPY", side="long", base_confidence=0.6, metadata=md
    ))
    assert ev.combined_direction > 0
    assert ev.combined_expected_edge_bps > 0
    assert 0.0 <= ev.aggregate_confidence <= 1.0
    assert ev.agreement == 1.0           # forecast/acc/news all positive
    assert ev.meta_label_probability == 0.58
    assert "price_forecast" in ev.contributing


def test_adapter_never_raises_on_garbage() -> None:
    for bad in ({}, {"forecast_expected_return": "n/a"}, {"accumulator_score": None}):
        fi = build_fusion_inputs_from_metadata(
            symbol="X", side="short", base_confidence=0.0, metadata=bad
        )
        fuse(fi)  # must not raise


def test_shadow_disabled_by_default_is_noop(monkeypatch) -> None:
    monkeypatch.delenv("FUSION_SHADOW", raising=False)
    assert fusion_shadow_enabled() is False
    # Must return cleanly (no exception, no effect) when disabled.
    asyncio.run(log_fusion_shadow_for_signal(
        symbol="SPY", side="long", confidence=0.5, metadata={"x": 1}, mode="hunter"
    ))


def test_shadow_enabled_runs_without_raising(monkeypatch) -> None:
    monkeypatch.setenv("FUSION_SHADOW", "1")
    assert fusion_shadow_enabled() is True
    asyncio.run(log_fusion_shadow_for_signal(
        symbol="SPY", side="long", confidence=0.7,
        metadata={"regime_label": "trend_up", "forecast_used": True,
                  "forecast_expected_return": 0.01, "forecast_confidence_blended": 0.6,
                  "meta_label_probability": 0.55},
        mode="hunter",
    ))
