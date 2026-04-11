"""Unit tests for D015 volume/flow feature + detection + scoring."""

from decimal import Decimal

from config.loaders import load_allocation
from core.models_runtime import VolumeAnomalyFeatures
from signals.volume_anomaly import (
    build_volume_anomaly_features_from_sources,
    detect_volume_flow,
    score_volume_anomaly_component,
)


def test_features_from_vol_ratio_and_metadata_z() -> None:
    f = build_volume_anomaly_features_from_sources(
        feature_json={"vol_ratio": 2.5, "vpin_proxy_50": 0.4},
        signal_metadata={"volume_z_score": 2.8},
    )
    assert f.volume_z == Decimal("2.8")
    assert f.relative_dollar_volume > Decimal("0.4")
    assert f.orderbook_imbalance == Decimal("0.4")


def test_detection_recommends_refresh_on_high_z() -> None:
    feat = VolumeAnomalyFeatures(volume_z=Decimal("3"), relative_dollar_volume=Decimal("0.2"))
    det = detect_volume_flow("BTC-USD", feat)
    assert det.refresh_context_recommended is True
    assert det.detection_strength > Decimal("0")


def test_fake_spike_dampens_strength() -> None:
    quiet_flow = build_volume_anomaly_features_from_sources(
        feature_json={"vol_ratio": 3.0, "vpin_proxy_50": 0.05},
        signal_metadata={"volume_z_score": Decimal("2")},
    )
    assert quiet_flow.fake_spike_penalty > Decimal("0")
    det = detect_volume_flow("X", quiet_flow)
    high_vol = build_volume_anomaly_features_from_sources(
        feature_json={"vol_ratio": 3.0, "vpin_proxy_50": 0.6},
        signal_metadata={"volume_z_score": Decimal("2")},
    )
    det_clear = detect_volume_flow("X", high_vol)
    assert det.detection_strength <= det_clear.detection_strength


def test_component_score_uses_allocation_weights() -> None:
    alloc = load_allocation()
    comp = alloc.opportunity_engine.components.volume_anomaly
    f = build_volume_anomaly_features_from_sources(
        feature_json={"vol_ratio": 2.2},
        signal_metadata={"volume_z_score": 1.5},
    )
    s = score_volume_anomaly_component(f, comp)
    assert Decimal("0") <= s <= Decimal("1")


def test_orderbook_zero_vpin_still_scores_other_terms() -> None:
    alloc = load_allocation()
    comp = alloc.opportunity_engine.components.volume_anomaly
    f = VolumeAnomalyFeatures(
        volume_z=Decimal("2"),
        relative_dollar_volume=Decimal("0.5"),
        orderbook_imbalance=Decimal("0"),
    )
    s = score_volume_anomaly_component(f, comp)
    assert s > Decimal("0")
