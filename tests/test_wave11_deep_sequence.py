"""
tests/test_wave11_deep_sequence.py
=====================================
Wave 11 acceptance tests.

Coverage:

- ``make_sequence_windows`` is leakage-safe: trailing rows whose
  horizon overruns are dropped; X[t] uses only features at or before t.
- ``RidgeSequenceBaseline.fit`` + ``predict`` round-trip; shape-mismatch
  predictions raise.
- Save/load round-trip for the baseline; feature-hash tampering detected.
- ``compare_against_baseline`` correctly sets ``deep_beats_baseline``
  in the three pass/fail regimes (deep wins clearly, deep loses on MSE,
  deep loses on net P&L).
- ``train_deep_sequence_model`` always trains the baseline; with
  ``architecture="none"``, ``promote_eligible=False`` and the
  comparison report is None.
- ``score_sequence`` returns ``no_artefact`` when given None; runs
  successfully on a fitted baseline.
- ``build_tcn`` / ``build_tft`` raise informative errors when torch is
  unavailable (default in this build).
- ``ai/fusion``-style architectural invariant: ``models/deep_sequence/``
  does NOT import ``brokers.*``.
"""

from __future__ import annotations

import ast
import pickle
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from models.deep_sequence import (
    BaselineComparisonReport,
    DeepSequenceConfig,
    RidgeSequenceBaseline,
    SequenceDataset,
    TrainedRidgeSequenceBaseline,
    compare_against_baseline,
    make_sequence_windows,
    score_sequence,
    train_deep_sequence_model,
)
from models.deep_sequence import build_tcn, build_tft
from models.deep_sequence.tcn import TCNSpec, torch_available as tcn_torch_available
from models.deep_sequence.tft import TFTSpec, torch_available as tft_torch_available


# ── windowing ─────────────────────────────────────────────────────────────


def _toy(n: int = 200, n_feat: int = 3, seed: int = 0):
    rng = np.random.default_rng(seed)
    F = rng.normal(0, 1, (n, n_feat))
    y = F[:, 0] * 0.5 + rng.normal(0, 0.1, n)  # signal in feature 0
    idx = pd.date_range("2026-01-01", periods=n, freq="h", tz="UTC")
    feats = pd.DataFrame(F, columns=[f"f{i}" for i in range(n_feat)], index=idx)
    target = pd.Series(y, index=idx)
    return feats, target


def test_make_sequence_windows_basic_shapes() -> None:
    feats, target = _toy(n=100, n_feat=4)
    ds = make_sequence_windows(feature_frame=feats, target=target, window=10, horizon=1)
    # n_samples == n_obs - window - horizon + 1 = 100 - 10 - 1 + 1 = 90 (when no NaN drops)
    assert ds.X.shape == (90, 10, 4)
    assert ds.y.shape == (90,)
    assert len(ds.timestamps) == 90


def test_make_sequence_windows_trailing_horizon_dropped() -> None:
    feats, target = _toy(n=50, n_feat=2)
    ds = make_sequence_windows(feature_frame=feats, target=target, window=5, horizon=3)
    # Last usable t = n - 1 - horizon = 50 - 1 - 3 = 46. First usable t = window-1 = 4.
    # Samples = 46 - 4 + 1 = 43.
    assert ds.X.shape[0] == 43


def test_make_sequence_windows_validates_inputs() -> None:
    feats, target = _toy(n=20, n_feat=1)
    with pytest.raises(ValueError):
        make_sequence_windows(feature_frame=feats, target=target, window=0, horizon=1)
    with pytest.raises(ValueError):
        make_sequence_windows(feature_frame=feats, target=target, window=5, horizon=-1)


def test_make_sequence_windows_no_leakage_in_x() -> None:
    feats, target = _toy(n=30, n_feat=2)
    ds = make_sequence_windows(feature_frame=feats, target=target, window=4, horizon=1)
    # Row 0 of the dataset corresponds to t = window-1 = 3 in the source.
    # Its X must equal feature_frame.iloc[0:4].values.
    np.testing.assert_allclose(ds.X[0], feats.iloc[0:4].to_numpy())
    # Its y must equal target.iloc[3 + 1] = target.iloc[4].
    assert ds.y[0] == pytest.approx(float(target.iloc[4]))


# ── baseline ──────────────────────────────────────────────────────────────


def test_ridge_baseline_fits_and_predicts() -> None:
    feats, target = _toy(n=300, n_feat=3, seed=1)
    ds = make_sequence_windows(feature_frame=feats, target=target, window=8, horizon=1)
    base = RidgeSequenceBaseline(alpha=1.0).fit(ds.X, ds.y)
    pred = base.predict(ds.X)
    assert pred.shape == (len(ds.y),)
    # Must correlate with target on training data (loose lower bound — not OOS).
    corr = float(np.corrcoef(pred, ds.y)[0, 1])
    assert corr > 0.05


def test_ridge_baseline_predict_shape_mismatch_raises() -> None:
    feats, target = _toy(n=80, n_feat=2)
    ds = make_sequence_windows(feature_frame=feats, target=target, window=5, horizon=1)
    base = RidgeSequenceBaseline().fit(ds.X, ds.y)
    bad = np.zeros((1, 7, 2))  # wrong window length
    with pytest.raises(ValueError, match="shape mismatch"):
        base.predict(bad)


def test_ridge_baseline_save_load(tmp_path: Path) -> None:
    feats, target = _toy(n=120, n_feat=2)
    ds = make_sequence_windows(feature_frame=feats, target=target, window=5, horizon=1)
    base = RidgeSequenceBaseline().fit(ds.X, ds.y)
    out = tmp_path / "base.pkl"
    base.save(out)
    loaded = TrainedRidgeSequenceBaseline.load(out)
    np.testing.assert_allclose(loaded.predict(ds.X[:3]), base.predict(ds.X[:3]))


def test_ridge_baseline_load_detects_hash_tampering(tmp_path: Path) -> None:
    from models.schemas import FeatureSpec

    feats, target = _toy(n=120, n_feat=2)
    ds = make_sequence_windows(feature_frame=feats, target=target, window=5, horizon=1)
    base = RidgeSequenceBaseline().fit(ds.X, ds.y)
    base.attach_feature_contract([FeatureSpec("f0", "float64"), FeatureSpec("f1", "float64")])
    out = tmp_path / "tampered.pkl"
    base.feature_contract_hash = "0" * 64
    with open(out, "wb") as f:
        pickle.dump(base, f)
    with pytest.raises(ValueError, match="hash mismatch"):
        TrainedRidgeSequenceBaseline.load(out)


# ── comparison harness ────────────────────────────────────────────────────


def test_compare_deep_beats_baseline_when_strictly_better() -> None:
    rng = np.random.default_rng(2)
    n = 200
    y = rng.normal(0, 1, n)
    # Baseline: random predictions; deep: nearly y itself (clear winner).
    baseline_pred = rng.normal(0, 1, n)
    deep_pred = y * 0.8 + rng.normal(0, 0.2, n)
    rep = compare_against_baseline(
        y_true=y, deep_predictions=deep_pred, baseline_predictions=baseline_pred,
        round_trip_cost_bps=0.0,  # zero cost so the net-PnL gate is a pure sign test
    )
    assert rep.deep_beats_baseline is True
    assert rep.failures == []


def test_compare_deep_loses_on_mse() -> None:
    rng = np.random.default_rng(3)
    n = 200
    y = rng.normal(0, 1, n)
    baseline_pred = y * 0.8 + rng.normal(0, 0.2, n)   # baseline tracks y
    deep_pred = rng.normal(0, 1, n)                    # random
    rep = compare_against_baseline(
        y_true=y, deep_predictions=deep_pred, baseline_predictions=baseline_pred,
    )
    assert rep.deep_beats_baseline is False
    assert any("mse_ratio" in f for f in rep.failures) or any("net_pnl_deep" in f for f in rep.failures)


def test_compare_deep_loses_on_net_pnl_when_costs_eat_edge() -> None:
    rng = np.random.default_rng(4)
    n = 100
    y = rng.normal(0, 0.001, n)  # tiny edge ⇒ costs dominate
    baseline_pred = np.zeros(n)  # never trades ⇒ zero P&L
    deep_pred = y + rng.normal(0, 0.0005, n)  # trades on every bar
    rep = compare_against_baseline(
        y_true=y, deep_predictions=deep_pred, baseline_predictions=baseline_pred,
        round_trip_cost_bps=200.0,  # 2% per round-trip — wipes any edge
    )
    assert rep.deep_beats_baseline is False
    # Specifically the net_pnl gate fired.
    assert any("net_pnl" in f for f in rep.failures)


def test_compare_empty_inputs_rejects() -> None:
    rep = compare_against_baseline(
        y_true=[], deep_predictions=[], baseline_predictions=[]
    )
    assert rep.deep_beats_baseline is False
    assert rep.failures == ["empty_oos"]


def test_compare_lengths_must_match() -> None:
    with pytest.raises(ValueError):
        compare_against_baseline(
            y_true=[1, 2, 3], deep_predictions=[1, 2], baseline_predictions=[1, 2, 3]
        )


# ── trainer ───────────────────────────────────────────────────────────────


def test_train_deep_sequence_baseline_only_when_architecture_none() -> None:
    feats, target = _toy(n=300, n_feat=3, seed=5)
    ds = make_sequence_windows(feature_frame=feats, target=target, window=10, horizon=1)
    cfg = DeepSequenceConfig(enabled=True, architecture="none")
    res = train_deep_sequence_model(dataset=ds, config=cfg)
    assert res.baseline is not None
    assert res.comparison is None
    assert res.promote_eligible is False
    assert "baseline" in res.notes.lower() or res.notes != ""


def test_train_deep_sequence_too_few_rows_raises() -> None:
    feats, target = _toy(n=20, n_feat=1)
    ds = make_sequence_windows(feature_frame=feats, target=target, window=5, horizon=1)
    cfg = DeepSequenceConfig(enabled=True, architecture="none")
    with pytest.raises(ValueError, match="insufficient rows"):
        train_deep_sequence_model(dataset=ds, config=cfg, holdout_fraction=0.95)


# ── infer ─────────────────────────────────────────────────────────────────


def test_score_sequence_no_artefact() -> None:
    res = score_sequence(artefact=None, sequence=np.zeros((5, 2)))
    assert res.used is False
    assert res.reason == "no_artefact"


def test_score_sequence_runs_on_fitted_baseline() -> None:
    feats, target = _toy(n=120, n_feat=2)
    ds = make_sequence_windows(feature_frame=feats, target=target, window=5, horizon=1)
    base = RidgeSequenceBaseline().fit(ds.X, ds.y)
    res = score_sequence(artefact=base, sequence=ds.X[0])
    assert res.used is True
    assert res.prediction is not None


def test_score_sequence_wrong_shape() -> None:
    feats, target = _toy(n=80, n_feat=2)
    ds = make_sequence_windows(feature_frame=feats, target=target, window=5, horizon=1)
    base = RidgeSequenceBaseline().fit(ds.X, ds.y)
    res = score_sequence(artefact=base, sequence=np.zeros((1, 2, 3, 4)))
    assert res.used is False
    assert res.reason == "wrong_shape"


# ── torch gating ──────────────────────────────────────────────────────────


def test_build_tcn_raises_when_torch_unavailable() -> None:
    # Phase B: the TCN stub was replaced with a real Bai-style torch module.
    # With torch present build_tcn now BUILDS (no longer NotImplementedError);
    # with torch absent it must still raise the clear "torch is required"
    # RuntimeError so callers degrade to the Ridge baseline.
    if tcn_torch_available():
        import torch

        model = build_tcn(TCNSpec(n_features=2, window=8, channels=(4, 4)))
        with torch.no_grad():
            out = model(torch.zeros(1, 8, 2))
        assert tuple(out.shape) == (1, 1)
        return
    with pytest.raises(RuntimeError, match="torch is required"):
        build_tcn(TCNSpec(n_features=2, window=8))


def test_build_tft_raises_when_torch_unavailable() -> None:
    if tft_torch_available():
        with pytest.raises((RuntimeError, NotImplementedError)):
            build_tft(TFTSpec(n_features=2, window=8))
        return
    with pytest.raises(RuntimeError, match="torch is required"):
        build_tft(TFTSpec(n_features=2, window=8))


# ── architectural invariant ───────────────────────────────────────────────


def test_deep_sequence_does_not_import_brokers() -> None:
    pkg_dir = Path("models/deep_sequence")
    offenders: list[str] = []
    for path in pkg_dir.glob("*.py"):
        src = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "brokers" or alias.name.startswith("brokers."):
                        offenders.append(f"{path.name}: import {alias.name}")
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if module == "brokers" or module.startswith("brokers."):
                    offenders.append(f"{path.name}: from {module} import ...")
    assert not offenders, f"models/deep_sequence imports brokers: {offenders}"


# ── default YAML loads disabled ──────────────────────────────────────────


def test_default_deep_sequence_yaml_is_disabled() -> None:
    import yaml

    raw = yaml.safe_load(Path("config/deep_sequence.yaml").read_text(encoding="utf-8"))
    assert (raw.get("deep_sequence") or {}).get("enabled") is False
    assert (raw.get("deep_sequence") or {}).get("architecture") == "none"
