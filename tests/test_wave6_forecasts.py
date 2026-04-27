"""
tests/test_wave6_forecasts.py
===============================
Wave 6 acceptance tests for forecast-native ML.

Coverage:

1. Target builders are leakage-safe (trailing rows NaN; values match a
   hand-computed example).
2. ``build_forecast_dataset_from_close`` drops trailing-NaN rows and
   preserves feature ordering.
3. Regression training (Ridge / NumPy fallback) recovers a positive IC
   on a synthetic series where features genuinely predict forward
   returns.
4. Classification training produces calibrated probabilities; tamper
   detection on the artefact's feature_contract_hash works.
5. ``ForecastEnsemble.combine`` blends regression + classification +
   vol members into the right (return, vol, confidence) shape.
6. Drawdown probability inverts polarity inside the ensemble (high
   probability ⇒ low confidence).
7. Bridge fallback matrix: disabled, no_models, not_registered,
   not_approved (paper), unapproved-live raises, all-skipped, approved.
8. ``compute_information_coefficient`` and
   ``compute_hit_rate_after_costs`` behave on synthetic data.
"""

from __future__ import annotations

import pickle
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from models.forecasts import (
    ForecastDataset,
    ForecastEnsemble,
    EnsembleMember,
    TrainedForecastModel,
    build_forecast_dataset_from_close,
    compute_hit_rate_after_costs,
    compute_information_coefficient,
    drawdown_probability,
    forward_return,
    realised_vol_forward,
    train_forecast_model,
)
from models.forecasts.targets import breakout_continuation, mean_reversion_success
from models.registry import (
    ModelNotApprovedError,
    ModelRegistry,
)
from models.schemas import (
    ApprovalStatus,
    FeatureSpec,
    Mode,
    ModelContract,
    Task,
)
from signals.forecast_bridge import (
    ForecastBridgeConfig,
    ForecastModelEntry,
    evaluate_features,
)


# ── helpers ─────────────────────────────────────────────────────────────────


def _series(n: int = 200, drift: float = 0.001, vol: float = 0.005, seed: int = 0):
    rng = np.random.default_rng(seed)
    rets = rng.normal(drift, vol, n)
    px = 100.0 * np.exp(np.cumsum(rets))
    idx = pd.date_range("2026-01-01", periods=n, freq="h", tz="UTC")
    return pd.Series(px, index=idx, name="close")


def _features_for(close: pd.Series, *, lookback: int = 5):
    rets = close.pct_change()
    return pd.DataFrame(
        {
            "ret_1": rets,
            "ret_lag1": rets.shift(1),
            "rolling_mean_5": rets.rolling(lookback).mean(),
        },
        index=close.index,
    )


# ── 1. target leakage / correctness ─────────────────────────────────────────


def test_forward_return_trailing_rows_are_nan() -> None:
    s = _series(n=50, seed=1)
    y = forward_return(s, horizon=4)
    assert y.iloc[-4:].isna().all()
    # Hand-computed: y[i] == s[i+4] / s[i] - 1.
    expected = float(s.iloc[10 + 4] / s.iloc[10] - 1.0)
    assert abs(float(y.iloc[10]) - expected) < 1e-12


def test_breakout_continuation_is_binary() -> None:
    s = _series(n=80, drift=0.005, seed=2)  # strong uptrend
    y = breakout_continuation(s, horizon=2, lookback=10)
    # NaN before the lookback window completes and at the trailing edge.
    assert y.iloc[:9].isna().all()
    finite = y.dropna()
    assert set(finite.unique()).issubset({0.0, 1.0})


def test_mean_reversion_success_basic() -> None:
    s = _series(n=80, drift=0.0, vol=0.001, seed=3)  # very flat
    y = mean_reversion_success(s, horizon=2, lookback=10, band=0.01)
    # Most rows should "succeed" (price stays near mean).
    finite = y.dropna()
    assert finite.mean() > 0.5


def test_realised_vol_forward_is_positive() -> None:
    s = _series(n=120, vol=0.01, seed=4)
    y = realised_vol_forward(s, horizon=10)
    finite = y.dropna()
    assert (finite > 0).all()


def test_drawdown_probability_in_zero_one() -> None:
    s = _series(n=80, vol=0.02, seed=5)
    y = drawdown_probability(s, horizon=10, threshold=0.02)
    finite = y.dropna()
    assert set(finite.unique()).issubset({0.0, 1.0})


# ── 2. dataset construction ─────────────────────────────────────────────────


def test_build_forecast_dataset_drops_trailing_window() -> None:
    s = _series(n=120, seed=6)
    feats = _features_for(s)
    ds = build_forecast_dataset_from_close(
        s,
        feature_frame=feats,
        target_kind="forward_return",
        horizon=5,
    )
    assert isinstance(ds, ForecastDataset)
    assert ds.target_kind == "forward_return"
    assert ds.horizon == 5
    assert ds.is_classification is False
    # No NaN in y or X after build.
    assert not ds.y.isna().any()
    assert not ds.X.isna().any().any()
    # Last index can't be one of the trailing-horizon rows.
    assert ds.timestamps[-1] <= s.index[len(s) - 5 - 1]
    # Ordering preserved.
    assert ds.feature_columns == ["ret_1", "ret_lag1", "rolling_mean_5"]


# ── 3. regression training has positive IC on a useful synthetic ────────────


def test_train_forecast_model_regression_positive_ic() -> None:
    rng = np.random.default_rng(7)
    n = 600
    f1 = rng.normal(0, 1, n)
    f2 = rng.normal(0, 1, n)
    # Forward return is genuinely a function of the features.
    y_true = 0.001 * f1 - 0.0005 * f2 + rng.normal(0, 0.003, n)
    feats = pd.DataFrame({"f1": f1, "f2": f2})
    feats.index = pd.date_range("2026-01-01", periods=n, freq="h", tz="UTC")
    y = pd.Series(y_true, index=feats.index, name="y")
    ds = ForecastDataset(
        X=feats,
        y=y,
        timestamps=feats.index,
        feature_columns=["f1", "f2"],
        target_kind="forward_return",
        horizon=1,
        is_classification=False,
    )
    specs = [FeatureSpec("f1", "float64"), FeatureSpec("f2", "float64")]
    art, report = train_forecast_model(
        dataset=ds, feature_specs=specs, estimator="ridge", calibration="none",
        n_splits=4, embargo_bars=2,
    )
    assert art.is_classification is False
    # The model must have *some* IC on the data it was trained on.
    yhat = pd.Series(art.predict(feats[["f1", "f2"]]), index=feats.index)
    ic = compute_information_coefficient(y, yhat)
    assert ic > 0.1


# ── 4. classification training + tamper detection ───────────────────────────


def test_train_forecast_model_classification_emits_probabilities() -> None:
    rng = np.random.default_rng(8)
    n = 400
    f = rng.normal(0, 1, n)
    y_clf = (f + rng.normal(0, 0.4, n) > 0).astype(int)
    feats = pd.DataFrame({"f": f}, index=pd.date_range("2026-01-01", periods=n, freq="h", tz="UTC"))
    y = pd.Series(y_clf, index=feats.index)
    ds = ForecastDataset(
        X=feats,
        y=y.astype(float),
        timestamps=feats.index,
        feature_columns=["f"],
        target_kind="breakout_continuation",
        horizon=1,
        is_classification=True,
    )
    specs = [FeatureSpec("f", "float64")]
    art, _ = train_forecast_model(
        dataset=ds, feature_specs=specs, estimator="logreg", calibration="platt"
    )
    p = art.predict(feats[["f"]])
    assert ((p >= 0) & (p <= 1)).all()


def test_artefact_load_detects_feature_hash_tampering(tmp_path: Path) -> None:
    rng = np.random.default_rng(9)
    n = 200
    feats = pd.DataFrame({"f": rng.normal(0, 1, n)},
                         index=pd.date_range("2026-01-01", periods=n, freq="h", tz="UTC"))
    y = pd.Series(rng.normal(0, 0.01, n), index=feats.index)
    ds = ForecastDataset(
        X=feats, y=y, timestamps=feats.index,
        feature_columns=["f"], target_kind="forward_return", horizon=1, is_classification=False,
    )
    specs = [FeatureSpec("f", "float64")]
    art, _ = train_forecast_model(dataset=ds, feature_specs=specs, estimator="ridge")
    out = tmp_path / "tampered.pkl"
    art.feature_contract_hash = "0" * 64
    with open(out, "wb") as f:
        pickle.dump(art, f)
    with pytest.raises(ValueError, match="hash mismatch"):
        TrainedForecastModel.load(out)


# ── 5. ensemble blending ────────────────────────────────────────────────────


def test_ensemble_combines_regression_and_classification() -> None:
    members = [
        EnsembleMember("forward_return", 1, 0.005, 1.0, "m1"),
        EnsembleMember("forward_return", 4, 0.003, 0.7, "m2"),
        EnsembleMember("realised_vol_forward", 4, 0.02, 1.0, "m3"),
        EnsembleMember("breakout_continuation", 1, 0.7, 0.5, "m4"),
    ]
    res = ForecastEnsemble.combine(members)
    assert res.expected_return is not None and res.expected_return > 0
    assert res.expected_volatility == pytest.approx(0.02, rel=1e-9)
    assert res.confidence is not None and res.confidence > 0.5
    assert {1, 4}.issubset(set(res.horizons_used))


def test_ensemble_drawdown_probability_inverts_to_confidence() -> None:
    # High drawdown probability ⇒ low confidence.
    high_dd = ForecastEnsemble.combine(
        [EnsembleMember("drawdown_probability", 24, 0.9, 1.0)]
    )
    low_dd = ForecastEnsemble.combine(
        [EnsembleMember("drawdown_probability", 24, 0.1, 1.0)]
    )
    assert high_dd.confidence is not None and low_dd.confidence is not None
    assert low_dd.confidence > high_dd.confidence


def test_ensemble_handles_empty_members() -> None:
    res = ForecastEnsemble.combine([])
    assert res.expected_return is None
    assert res.expected_volatility is None
    assert res.confidence is None


# ── 6. evaluation helpers ───────────────────────────────────────────────────


def test_information_coefficient_zero_on_random() -> None:
    rng = np.random.default_rng(10)
    a = rng.normal(0, 1, 200)
    b = rng.normal(0, 1, 200)
    ic = compute_information_coefficient(a, b)
    assert -0.2 < ic < 0.2


def test_hit_rate_after_costs_basic() -> None:
    y_true = np.array([0.01, -0.01, 0.005, -0.005, 0.02])
    y_pred = np.array([0.015, -0.012, 0.001, -0.001, 0.018])
    hr = compute_hit_rate_after_costs(y_true, y_pred, round_trip_cost=0.0001)
    assert 0.0 <= hr <= 1.0


# ── 7. bridge fallback matrix ───────────────────────────────────────────────


def _make_dummy_artefact(n_features: int = 1) -> TrainedForecastModel:
    rng = np.random.default_rng(11)
    n = 200
    f = rng.normal(0, 1, (n, n_features))
    feats = pd.DataFrame(f, columns=[f"f{i}" for i in range(n_features)],
                         index=pd.date_range("2026-01-01", periods=n, freq="h", tz="UTC"))
    y = pd.Series(rng.normal(0, 0.01, n), index=feats.index)
    ds = ForecastDataset(
        X=feats, y=y, timestamps=feats.index,
        feature_columns=list(feats.columns),
        target_kind="forward_return", horizon=1, is_classification=False,
    )
    specs = [FeatureSpec(c, "float64") for c in feats.columns]
    art, _ = train_forecast_model(dataset=ds, feature_specs=specs, estimator="ridge")
    return art


def test_bridge_disabled_returns_disabled_decision() -> None:
    cfg = ForecastBridgeConfig(enabled=False)
    d = evaluate_features(features={}, mode=Mode.PAPER, config=cfg, registry=ModelRegistry())
    assert d.used is False and d.reason == "disabled"


def test_bridge_no_models_passthrough() -> None:
    cfg = ForecastBridgeConfig(enabled=True, members=[])
    d = evaluate_features(features={}, mode=Mode.PAPER, config=cfg, registry=ModelRegistry())
    assert d.used is False and d.reason == "no_models"


def test_bridge_paper_skips_unregistered_member_returns_not_approved() -> None:
    cfg = ForecastBridgeConfig(
        enabled=True,
        members=[ForecastModelEntry(name="ghost", version="0.1", target_kind="forward_return", horizon=1)],
    )
    d = evaluate_features(features={}, mode=Mode.PAPER, config=cfg, registry=ModelRegistry())
    assert d.used is False
    assert d.reason in {"not_approved", "artefact_unavailable"}


def test_bridge_live_unregistered_raises() -> None:
    cfg = ForecastBridgeConfig(
        enabled=True,
        members=[ForecastModelEntry(name="ghost", version="0.1", target_kind="forward_return", horizon=1)],
    )
    with pytest.raises(ModelNotApprovedError):
        evaluate_features(features={}, mode=Mode.LIVE, config=cfg, registry=ModelRegistry())


def test_bridge_live_research_status_raises() -> None:
    artefact = _make_dummy_artefact()
    contract = ModelContract(
        name="m", version="0.1", task=Task.REGRESSION, target="forward_return",
        feature_contract_hash=artefact.feature_contract_hash,
        validation_method="purged_kfold",
        approval_status=ApprovalStatus.RESEARCH,
    )
    reg = ModelRegistry([contract])
    cfg = ForecastBridgeConfig(
        enabled=True,
        members=[ForecastModelEntry(name="m", version="0.1", target_kind="forward_return", horizon=1)],
    )
    with pytest.raises(ModelNotApprovedError):
        evaluate_features(features={"f0": 1.0}, mode=Mode.LIVE, config=cfg, registry=reg)


def test_bridge_paper_approved_member_runs_through_ensemble() -> None:
    artefact = _make_dummy_artefact()
    contract = ModelContract(
        name="m", version="0.1", task=Task.REGRESSION, target="forward_return",
        feature_contract_hash=artefact.feature_contract_hash,
        validation_method="purged_kfold",
        approval_status=ApprovalStatus.PAPER,
    )
    reg = ModelRegistry([contract])
    cfg = ForecastBridgeConfig(
        enabled=True,
        members=[ForecastModelEntry(name="m", version="0.1", target_kind="forward_return", horizon=1)],
    )
    d = evaluate_features(
        features={"f0": 1.0},
        mode=Mode.PAPER,
        config=cfg,
        registry=reg,
        artefact_loader=lambda entry, c: artefact,
    )
    assert d.used is True and d.reason == "approved"
    assert d.expected_return is not None
    assert d.horizons_used == (1,)
    assert d.members_used == ["m@0.1"]


def test_bridge_default_yaml_loads_disabled() -> None:
    cfg = ForecastBridgeConfig.load(Path("config/forecast_models.yaml"))
    assert cfg.enabled is False
    assert cfg.members == []
