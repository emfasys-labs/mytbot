from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import numpy as np
import pandas as pd

from config.loaders import load_allocation
from core.models_runtime import PortfolioState
from risk.regime_state import (
    _RegimeTransitionGate,
    compute_regime_state_from_inputs,
    reset_regime_classifier_cache,
)
from risk.regime_transition import RegimeTransitionDetector
from scripts.train_regime_transition import build_transition_dataset
from scripts.report_phase_c_transition import _load_transition_config
from scripts.evaluate_phase_c_transition_history import score_transition_rows
from scripts.simulate_phase_c_allocator_impact import simulate_allocator_impact
from scripts.sweep_phase_c_allocator_policy import sweep_allocator_policies
from scripts.phase_c_readiness import summarize_readiness
from scripts.export_phase_c_shadow_history import write_shadow_history
from scripts.phase_c_status_bundle import COMMANDS


def test_regime_transition_detector_save_load_roundtrip(tmp_path: Path) -> None:
    rng = np.random.default_rng(1)
    X = rng.normal(size=(200, 3))
    y = (X[:, 0] - X[:, 1] > 0).astype(int)
    det = RegimeTransitionDetector(feature_names=("a", "b", "c"), threshold=0.55).fit(X, y)
    pred = det.predict(X[0])
    assert 0.0 <= pred.probability <= 1.0
    p = tmp_path / "det.pkl"
    det.save(p)
    loaded = RegimeTransitionDetector.load(p)
    assert loaded.feature_names == ("a", "b", "c")
    assert abs(loaded.predict_probability(X[0]) - det.predict_probability(X[0])) < 1e-12


def test_build_transition_dataset_from_panel_history() -> None:
    idx = pd.date_range("2024-01-01", periods=180, freq="h", tz="UTC")
    rows = []
    for sym_i, sym in enumerate(["SPY", "QQQ", "BTC-USD", "TLT", "GLD", "NVDA"]):
        for i, ts in enumerate(idx):
            close = 100 + sym_i * 5 + i * 0.1
            rows.append(
                {
                    "symbol": sym,
                    "bar_timestamp": ts,
                    "close": close,
                    "features": {
                        "mom_10": float(i % 7 - 3),
                        "rsi_14": 50.0 + float(i % 10),
                        "volume_z": float((i % 5) / 2),
                        "relative_dollar_volume": 1.0 + float(i % 3) / 10,
                        "garch_vol_1d": 0.01,
                    },
                }
            )
    X, y, audit = build_transition_dataset(
        pd.DataFrame(rows),
        horizon=4,
        min_symbols_per_bar=4,
    )
    assert len(X) > 100
    assert len(X) == len(y) == len(audit)
    assert set(X.columns) == {
        "trend_strength",
        "breadth_score",
        "market_state_score",
        "chaos_penalty",
        "volatility_structure",
        "anomaly_breadth",
        "correlation_crowding",
        "liquidity_state",
        "news_conflict_score",
    }


def test_transition_shadow_metadata_is_inert(monkeypatch) -> None:
    reset_regime_classifier_cache()

    class _Stub:
        feature_names = (
            "trend_strength",
            "breadth_score",
            "market_state_score",
            "chaos_penalty",
            "volatility_structure",
            "anomaly_breadth",
            "correlation_crowding",
            "liquidity_state",
            "news_conflict_score",
        )

        def predict(self, x):  # noqa: ANN001
            from risk.regime_transition import RegimeTransitionPrediction

            return RegimeTransitionPrediction(
                probability=0.9,
                label="stress_transition",
                threshold=0.6,
                model_version="test",
            )

    monkeypatch.setattr(
        "risk.regime_state._load_regime_transition_gate",
        lambda: _RegimeTransitionGate(enabled=True, shadow_only=True, artifact_path=Path("x"), threshold=0.6),
    )
    monkeypatch.setattr("risk.regime_state._load_regime_transition_artefact", lambda gate: _Stub())
    now = datetime.now(timezone.utc)
    portfolio = PortfolioState(
        timestamp=now,
        mode="hunter",
        nav=Decimal("100000"),
        cash=Decimal("50000"),
        available_buying_power=Decimal("50000"),
        gross_exposure=Decimal("50000"),
        net_exposure=Decimal("50000"),
        leverage_ratio=Decimal("1"),
        drawdown_from_hwm_pct=Decimal("0"),
    )
    rows = [
        {"symbol": "A", "features": {"mom_10": 2.0, "rsi_14": 60.0, "volume_z": 0.5, "relative_dollar_volume": 1.1}},
        {"symbol": "B", "features": {"mom_10": 1.0, "rsi_14": 57.0, "volume_z": 0.3, "relative_dollar_volume": 1.0}},
        {"symbol": "C", "features": {"mom_10": 1.5, "rsi_14": 59.0, "volume_z": 0.2, "relative_dollar_volume": 1.2}},
    ]
    r = compute_regime_state_from_inputs(
        portfolio_state=portfolio,
        allocation_cfg=load_allocation(),
        feature_rows=rows,
        news_dispersion=None,
    )
    assert r.metadata["regime_transition_used"] is True
    assert r.metadata["regime_transition_shadow_only"] is True
    assert r.metadata["regime_transition_probability"] == 0.9
    assert r.regime_label != "crash"  # transition shadow does not override label


def test_active_transition_detector_throttles_allocation(monkeypatch) -> None:
    reset_regime_classifier_cache()

    class _Stub:
        feature_names = (
            "trend_strength",
            "breadth_score",
            "market_state_score",
            "chaos_penalty",
            "volatility_structure",
            "anomaly_breadth",
            "correlation_crowding",
            "liquidity_state",
            "news_conflict_score",
        )

        def predict(self, x):  # noqa: ANN001
            from risk.regime_transition import RegimeTransitionPrediction

            return RegimeTransitionPrediction(
                probability=0.9,
                label="stress_transition",
                threshold=0.6,
                model_version="test",
            )

    monkeypatch.setattr(
        "risk.regime_state._load_regime_transition_gate",
        lambda: _RegimeTransitionGate(enabled=True, shadow_only=False, artifact_path=Path("x"), threshold=0.6),
    )
    monkeypatch.setattr("risk.regime_state._load_regime_transition_artefact", lambda gate: _Stub())
    now = datetime.now(timezone.utc)
    portfolio = PortfolioState(
        timestamp=now,
        mode="hunter",
        nav=Decimal("100000"),
        cash=Decimal("50000"),
        available_buying_power=Decimal("50000"),
        gross_exposure=Decimal("50000"),
        net_exposure=Decimal("50000"),
        leverage_ratio=Decimal("1"),
        drawdown_from_hwm_pct=Decimal("0"),
    )
    rows = [
        {"symbol": "A", "features": {"mom_10": 2.0, "rsi_14": 60.0, "volume_z": 0.5, "relative_dollar_volume": 1.1}},
        {"symbol": "B", "features": {"mom_10": 1.0, "rsi_14": 57.0, "volume_z": 0.3, "relative_dollar_volume": 1.0}},
        {"symbol": "C", "features": {"mom_10": 1.5, "rsi_14": 59.0, "volume_z": 0.2, "relative_dollar_volume": 1.2}},
    ]
    r = compute_regime_state_from_inputs(
        portfolio_state=portfolio,
        allocation_cfg=load_allocation(),
        feature_rows=rows,
        news_dispersion=None,
    )
    assert r.metadata["regime_transition_shadow_only"] is False
    assert r.metadata["regime_transition_active_multiplier"] == 0.1
    assert r.drawdown_throttle == Decimal("0.1")


def test_phase_c_report_loads_transition_config(tmp_path: Path) -> None:
    cfg = tmp_path / "regime_models.yaml"
    cfg.write_text(
        """
regime_models:
  transition_detector:
    enabled: true
    shadow_only: true
    artifact_path: artifacts/x.pkl
    threshold: 0.45
    feature_names: [trend_strength, breadth_score]
""".strip(),
        encoding="utf-8",
    )

    out = _load_transition_config(cfg)

    assert out["enabled"] is True
    assert out["threshold"] == 0.45
    assert out["feature_names"] == ["trend_strength", "breadth_score"]


def test_phase_c_history_score_counts_confusion_matrix() -> None:
    rows = [
        {"predicted_stress": True, "actual_stress": True},
        {"predicted_stress": True, "actual_stress": False},
        {"predicted_stress": False, "actual_stress": False},
        {"predicted_stress": False, "actual_stress": True},
        {"predicted_stress": True, "actual_stress": None},
    ]

    out = score_transition_rows(rows)

    assert out["evaluated"] == 4
    assert out["tp"] == 1
    assert out["fp"] == 1
    assert out["tn"] == 1
    assert out["fn"] == 1
    assert out["precision"] == 0.5
    assert out["recall"] == 0.5
    assert out["accuracy"] == 0.5


def test_phase_c_allocator_impact_simulation_separates_avoided_loss_and_missed_gain() -> None:
    rows = [
        {"probability": 0.7, "future_panel_return": -0.02},
        {"probability": 0.8, "future_panel_return": 0.01},
        {"probability": 0.2, "future_panel_return": 0.03},
        {"probability": None, "future_panel_return": -0.01},
    ]

    out = simulate_allocator_impact(rows, trigger_probability=0.55, throttle_multiplier=0.5)

    assert out["evaluated"] == 3
    assert out["throttled"] == 2
    assert round(out["baseline_return_sum"], 6) == 0.02
    assert round(out["simulated_return_sum"], 6) == 0.025
    assert round(out["impact_return_sum"], 6) == 0.005
    assert round(out["avoided_loss"], 6) == 0.01
    assert round(out["missed_gain"], 6) == 0.005


def test_phase_c_policy_sweep_ranks_positive_impact_first() -> None:
    rows = [
        {"probability": 0.7, "future_panel_return": -0.02},
        {"probability": 0.4, "future_panel_return": 0.01},
    ]

    out = sweep_allocator_policies(
        rows,
        trigger_probabilities=[0.5, 0.8],
        throttle_multipliers=[0.5],
    )

    assert out[0]["trigger_probability"] == 0.5
    assert out[0]["impact_return_sum"] > out[1]["impact_return_sum"]


def test_phase_c_readiness_counts_mature_rows() -> None:
    now = datetime(2026, 5, 16, 12, 0, tzinfo=timezone.utc)
    rows = [
        {"timestamp": "2026-05-16T07:00:00+00:00"},
        {"timestamp": "2026-05-16T09:00:00+00:00"},
    ]

    out = summarize_readiness(rows, now=now, horizon=timedelta(hours=4), min_mature_rows=2)

    assert out["history_rows"] == 2
    assert out["mature_rows"] == 1
    assert out["ready"] is False
    assert out["next_ready_at"] == "2026-05-16T13:00:00+00:00"


def test_phase_c_shadow_history_export_writes_csv_and_json(tmp_path: Path) -> None:
    rows = [{"timestamp": "t", "probability": 0.5, "policy_exposure_multiplier": 1.0}]

    csv_path, json_path = write_shadow_history(rows, out_dir=tmp_path, prefix="sample")

    assert csv_path.exists()
    assert json_path.exists()
    assert "policy_exposure_multiplier" in csv_path.read_text(encoding="utf-8")
    assert '"probability": 0.5' in json_path.read_text(encoding="utf-8")


def test_phase_c_status_bundle_includes_expected_scripts() -> None:
    joined = [" ".join(cmd) for cmd in COMMANDS]
    assert any("phase_c_readiness.py" in cmd for cmd in joined)
    assert any("report_phase_c_transition.py" in cmd for cmd in joined)
    assert any("evaluate_phase_c_transition_history.py" in cmd for cmd in joined)
    assert any("sweep_phase_c_allocator_policy.py" in cmd for cmd in joined)
    assert any("export_phase_c_shadow_history.py" in cmd for cmd in joined)
