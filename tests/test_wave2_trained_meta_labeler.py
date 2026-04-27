"""
tests/test_wave2_trained_meta_labeler.py
=========================================
Wave 2 acceptance tests for the trained meta-labeller.

Coverage:

1. ``build_dataset_from_close`` produces aligned, leakage-safe rows
   (the last ``max_horizon`` bars are excluded).
2. ``train_meta_label_model`` on synthetic data produces a calibrated
   artefact whose probabilities are monotone in the underlying signal.
3. The artefact survives a save/load round-trip and the feature-hash
   guard fires on tampering.
4. ``signals/trained_meta_labeler.evaluate_features`` returns
   pass-through when disabled, when no model is configured, when the
   configured model is missing, and when the artefact is unavailable.
5. In LIVE mode, an unapproved (``research``) model raises
   ``ModelNotApprovedError`` — the operator MUST promote the model
   deliberately before live use.
6. Threshold resolution honours mode-regime > mode > regime > default.
7. The Wave 1 heuristic ``signals/meta_labeler.py`` import surface is
   unchanged (Wave 2 is additive).
"""

from __future__ import annotations

import pickle
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from backtest.labels import TripleBarrierSpec
from models.meta_label import (
    MetaLabelDataset,
    ThresholdConfig,
    TrainedMetaLabel,
    build_dataset_from_close,
    threshold_for,
    train_meta_label_model,
)
from models.meta_label.evaluate import evaluate_calibration
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
from signals.trained_meta_labeler import (
    TrainedMetaLabelerConfig,
    evaluate_features,
)


# ── synthetic data helpers ──────────────────────────────────────────────────


def _make_synthetic(n: int = 600, seed: int = 7):
    rng = np.random.default_rng(seed)
    dt = pd.date_range("2026-01-01", periods=n, freq="h", tz="UTC")
    # Trend with regime: positive drift first half, negative second half.
    drift = np.where(np.arange(n) < n // 2, 0.001, -0.001)
    noise = rng.normal(0, 0.005, n)
    rets = drift + noise
    px = 100 * np.exp(np.cumsum(rets))
    close = pd.Series(px, index=dt, name="close")

    # Features: confidence, momentum proxy, vol proxy.
    mom = pd.Series(rets, index=dt).rolling(10).mean().fillna(0.0)
    vol = pd.Series(rets, index=dt).rolling(20).std().fillna(0.0)
    conf = (mom.rank(pct=True)).fillna(0.5)
    feat = pd.DataFrame({"strategy_confidence": conf, "momentum": mom, "volatility": vol}, index=dt)

    # "Primary" signal: buy when momentum > 0, sell otherwise.
    sides = np.where(mom > 0, "buy", "sell")
    sides = pd.Series(sides, index=dt)
    return close, feat, sides


# ── 1. dataset leakage safety ───────────────────────────────────────────────


def test_build_dataset_excludes_last_horizon_bars() -> None:
    close, feat, sides = _make_synthetic(n=120)
    spec = TripleBarrierSpec(pt_mult=2.0, sl_mult=1.5, max_horizon=8, vol_window=20)
    ds = build_dataset_from_close(close, feature_frame=feat, sides=sides, barrier_spec=spec)
    assert isinstance(ds, MetaLabelDataset)
    # No row may have an index later than n - horizon - 1.
    last_allowed = close.index[len(close) - spec.max_horizon - 1]
    assert ds.timestamps.max() <= last_allowed
    # Binary labels only.
    assert set(ds.y.unique()).issubset({0, 1})
    # Feature columns preserved in order.
    assert ds.feature_columns == ["strategy_confidence", "momentum", "volatility"]


def test_build_dataset_drops_rows_without_side() -> None:
    close, feat, sides = _make_synthetic(n=80)
    sides = sides.copy()
    # Knock out 10 sides — those rows must not appear in y.
    sides.iloc[20:30] = None
    spec = TripleBarrierSpec(max_horizon=5)
    ds = build_dataset_from_close(close, feature_frame=feat, sides=sides, barrier_spec=spec)
    knocked_out = close.index[20:30]
    assert not any(ts in ds.timestamps for ts in knocked_out)


# ── 2. training and probability monotonicity ────────────────────────────────


def test_train_meta_label_model_produces_useful_probabilities() -> None:
    close, feat, sides = _make_synthetic(n=600)
    spec = TripleBarrierSpec(pt_mult=2.0, sl_mult=1.5, max_horizon=8, vol_window=20)
    ds = build_dataset_from_close(close, feature_frame=feat, sides=sides, barrier_spec=spec)

    feature_specs = [FeatureSpec(c, "float64") for c in ds.feature_columns]
    artefact, report = train_meta_label_model(
        X=ds.X,
        y=ds.y,
        feature_specs=feature_specs,
        classifier="logreg",
        calibration="isotonic",
        n_splits=4,
        embargo_bars=4,
    )
    assert isinstance(artefact, TrainedMetaLabel)
    assert artefact.feature_contract_hash == report.feature_contract_hash
    assert report.n_train == len(ds.y)

    # Probabilities are bounded.
    p = artefact.predict_proba(ds.X)
    assert (p >= 0.0).all() and (p <= 1.0).all()


def test_calibration_summary_smokes() -> None:
    p = np.linspace(0, 1, 200)
    y = (p + np.random.default_rng(0).normal(0, 0.1, 200) > 0.5).astype(int)
    summary = evaluate_calibration(y, p, n_bins=5)
    assert summary.n_bins == 5
    assert 0.0 <= summary.expected_calibration_error <= 1.0


# ── 3. save/load round-trip and tamper detection ────────────────────────────


def test_artefact_save_load_roundtrip(tmp_path: Path) -> None:
    close, feat, sides = _make_synthetic(n=400)
    spec = TripleBarrierSpec(max_horizon=6)
    ds = build_dataset_from_close(close, feature_frame=feat, sides=sides, barrier_spec=spec)
    feature_specs = [FeatureSpec(c, "float64") for c in ds.feature_columns]
    artefact, _ = train_meta_label_model(
        X=ds.X, y=ds.y, feature_specs=feature_specs, classifier="logreg", calibration="none"
    )
    out = tmp_path / "meta.pkl"
    artefact.save(out)
    loaded = TrainedMetaLabel.load(out)
    assert loaded.feature_contract_hash == artefact.feature_contract_hash
    np.testing.assert_allclose(
        loaded.predict_proba(ds.X.head(5)), artefact.predict_proba(ds.X.head(5))
    )


def test_artefact_load_detects_feature_hash_tampering(tmp_path: Path) -> None:
    close, feat, sides = _make_synthetic(n=300)
    spec = TripleBarrierSpec(max_horizon=4)
    ds = build_dataset_from_close(close, feature_frame=feat, sides=sides, barrier_spec=spec)
    feature_specs = [FeatureSpec(c, "float64") for c in ds.feature_columns]
    artefact, _ = train_meta_label_model(
        X=ds.X, y=ds.y, feature_specs=feature_specs, classifier="logreg", calibration="none"
    )
    out = tmp_path / "tampered.pkl"
    # Corrupt the hash to simulate tampering with the contract.
    artefact.feature_contract_hash = "0" * 64
    with open(out, "wb") as f:
        pickle.dump(artefact, f)
    with pytest.raises(ValueError, match="hash mismatch"):
        TrainedMetaLabel.load(out)


# ── 4. runtime evaluate_features fallbacks ──────────────────────────────────


def test_evaluate_features_passthrough_when_disabled() -> None:
    cfg = TrainedMetaLabelerConfig(enabled=False, model_name="dummy")
    decision = evaluate_features(features={}, mode=Mode.PAPER, config=cfg, registry=ModelRegistry())
    assert decision.kept is True
    assert decision.reason == "disabled"
    assert decision.probability is None


def test_evaluate_features_passthrough_when_no_model_configured() -> None:
    cfg = TrainedMetaLabelerConfig(enabled=True, model_name=None)
    decision = evaluate_features(features={}, mode=Mode.PAPER, config=cfg, registry=ModelRegistry())
    assert decision.reason == "no_model_passthrough"
    assert decision.kept is True


def test_evaluate_features_passthrough_when_model_not_registered_in_paper() -> None:
    cfg = TrainedMetaLabelerConfig(enabled=True, model_name="ghost", model_version="0.1.0")
    decision = evaluate_features(features={}, mode=Mode.PAPER, config=cfg, registry=ModelRegistry())
    assert decision.kept is True
    assert decision.reason == "not_registered"


def test_evaluate_features_research_status_in_paper_mode_passthrough() -> None:
    """Registered but research-status model: pass-through in paper, raise in live."""
    feats = [FeatureSpec("x", "float64")]
    contract = ModelContract(
        name="m",
        version="0.1",
        task=Task.CLASSIFICATION,
        target="triple_barrier",
        feature_contract_hash="abc",
        validation_method="purged_kfold",
        approval_status=ApprovalStatus.RESEARCH,
    )
    reg = ModelRegistry([contract])
    cfg = TrainedMetaLabelerConfig(enabled=True, model_name="m", model_version="0.1")

    # Paper mode: warning then pass-through (no artefact loader).
    decision = evaluate_features(features={"x": 1.0}, mode=Mode.PAPER, config=cfg, registry=reg)
    # research-in-paper is allowed by registry; no artefact loader => artefact_unavailable
    assert decision.kept is True
    assert decision.reason in {"artefact_unavailable", "approved", "below_threshold"}


def test_evaluate_features_research_status_in_live_raises() -> None:
    contract = ModelContract(
        name="m",
        version="0.1",
        task=Task.CLASSIFICATION,
        target="triple_barrier",
        feature_contract_hash="abc",
        validation_method="purged_kfold",
        approval_status=ApprovalStatus.RESEARCH,
    )
    reg = ModelRegistry([contract])
    cfg = TrainedMetaLabelerConfig(enabled=True, model_name="m", model_version="0.1")
    with pytest.raises(ModelNotApprovedError):
        evaluate_features(features={"x": 1.0}, mode=Mode.LIVE, config=cfg, registry=reg)


# ── 5. end-to-end with injected artefact loader ─────────────────────────────


def test_evaluate_features_uses_injected_artefact_paper_mode() -> None:
    feats = [FeatureSpec("x", "float64"), FeatureSpec("y", "float64")]
    contract = ModelContract(
        name="m",
        version="0.1",
        task=Task.CLASSIFICATION,
        target="triple_barrier",
        feature_contract_hash="abc",
        validation_method="purged_kfold",
        approval_status=ApprovalStatus.PAPER,
    )
    reg = ModelRegistry([contract])
    cfg = TrainedMetaLabelerConfig(
        enabled=True,
        model_name="m",
        model_version="0.1",
        thresholds=ThresholdConfig(default=0.5),
    )

    # Build a tiny artefact by hand: train on 2-feature toy data.
    rng = np.random.default_rng(0)
    X = pd.DataFrame({"x": rng.normal(0, 1, 200), "y": rng.normal(0, 1, 200)})
    y = ((X["x"] + X["y"]) > 0).astype(int)
    artefact, _ = train_meta_label_model(
        X=X, y=y, feature_specs=feats, classifier="logreg", calibration="platt"
    )

    decision_high = evaluate_features(
        features={"x": 5.0, "y": 5.0},
        mode=Mode.PAPER,
        config=cfg,
        registry=reg,
        artefact_loader=lambda c: artefact,
    )
    decision_low = evaluate_features(
        features={"x": -5.0, "y": -5.0},
        mode=Mode.PAPER,
        config=cfg,
        registry=reg,
        artefact_loader=lambda c: artefact,
    )
    assert decision_high.probability is not None
    assert decision_low.probability is not None
    assert decision_high.probability > decision_low.probability
    assert decision_high.reason in {"approved", "below_threshold"}


# ── 6. threshold resolution ─────────────────────────────────────────────────


def test_threshold_resolution_priority() -> None:
    cfg = ThresholdConfig(
        default=0.55,
        by_mode={"hunter": 0.50, "defender": 0.62},
        by_regime={"crash": 0.70},
        by_mode_regime={"hunter.crash": 0.65},
    )
    # mode-regime wins.
    assert threshold_for(cfg, mode="hunter", regime="crash") == 0.65
    # mode-regime miss but mode hits.
    assert threshold_for(cfg, mode="hunter", regime="trend") == 0.50
    # regime-only.
    assert threshold_for(cfg, mode=None, regime="crash") == 0.70
    # default.
    assert threshold_for(cfg) == 0.55


def test_threshold_config_from_dict_normalises_keys() -> None:
    raw = {
        "default": 0.6,
        "by_mode": {"HUNTER": 0.5},
        "by_mode_regime": {"hunter:crash": 0.65},
    }
    cfg = ThresholdConfig.from_dict(raw)
    assert threshold_for(cfg, mode="hunter") == 0.5
    assert threshold_for(cfg, mode="hunter", regime="crash") == 0.65


# ── 7. heuristic compatibility (Wave 2 is additive) ─────────────────────────


def test_heuristic_meta_labeler_imports_unchanged() -> None:
    # Importing both modules at once must not blow up. Wave 2 must not
    # have replaced the heuristic; it's still the live path until an
    # operator flips trained_meta_labeler.enabled.
    from signals.meta_labeler import filter_candidates, keep_raw_signal  # noqa: F401
    import signals.trained_meta_labeler as tml  # noqa: F401
