"""Phase B: real TCN body + forecast-bridge untrained-model safety gate.

Torch-independent where it matters: the safety gate (the critical bit) is
tested without torch; the TCN forward is smoke-tested only when torch is
present. Everything stays inert by default (forecast_bridge disabled).
"""

from __future__ import annotations

import importlib.util

import pytest

from models.deep_sequence.tcn import TCNSpec, build_tcn, torch_available

_HAS_TORCH = importlib.util.find_spec("torch") is not None


def test_build_tcn_requires_torch_or_builds() -> None:
    if not torch_available():
        with pytest.raises(RuntimeError, match="torch is required"):
            build_tcn(TCNSpec(n_features=4, window=8))
        return
    m = build_tcn(TCNSpec(n_features=4, window=8, channels=(8, 8), output_dim=1))
    assert m is not None


@pytest.mark.skipif(not _HAS_TORCH, reason="torch not installed")
def test_tcn_forward_shape_and_causal() -> None:
    import torch

    spec = TCNSpec(n_features=5, window=12, channels=(8, 8), output_dim=1)
    m = build_tcn(spec).eval()
    x = torch.randn(3, 12, 5)
    with torch.no_grad():
        y = m(x)
    assert tuple(y.shape) == (3, 1)

    # Causality: perturbing ONLY the last timestep must change the output;
    # perturbing only a future-padded region cannot leak (already enforced
    # structurally) — here we assert the model actually uses the sequence.
    x2 = x.clone()
    x2[:, -1, :] += 5.0
    with torch.no_grad():
        y2 = m(x2)
    assert not torch.allclose(y, y2)


def test_forecast_bridge_safety_gate_skips_unvalidated_deep_model() -> None:
    """A deep/sequence artefact lacking deep_beats_baseline=True must be
    skipped in PAPER (used=False), never scored. Tabular artefacts (no
    model_kind marker) are unaffected — covered by existing bridge tests."""
    from types import SimpleNamespace

    from signals.forecast_bridge import (
        ForecastBridgeConfig,
        ForecastModelEntry,
        evaluate_features,
    )
    from models.schemas import Mode

    class _Artefact:
        model_kind = "tcn_sequence"
        metadata = {"model_kind": "tcn_sequence"}  # NO deep_beats_baseline
        target_kind = "forward_return"
        horizon = 1
        feature_specs: list = []

    contract = SimpleNamespace(name="seq_fc", version="0.0.1")

    class _Reg:
        def require_for_mode(self, name, *, mode, version=None):  # noqa: ANN001
            return contract

    cfg = ForecastBridgeConfig(
        enabled=True,
        members=[ForecastModelEntry(name="seq_fc", version="0.0.1")],
    )
    decision = evaluate_features(
        features={"f": 1.0},
        mode=Mode.PAPER,
        config=cfg,
        registry=_Reg(),
        artefact_loader=lambda *_a, **_k: _Artefact(),
    )
    assert decision.used is False
    assert decision.metadata["skipped"]["seq_fc"] == "deep_not_baseline_validated"


@pytest.mark.skipif(not _HAS_TORCH, reason="torch not installed")
def test_train_artefact_pipeline_is_honest_and_gated() -> None:
    """End-to-end: TCN trains, the comparison harness runs HONESTLY, the
    artefact records the real verdict, and an artefact that did not beat the
    baseline OOS is REFUSED on load (defence in depth). On a trivial linear
    toy the Ridge baseline is expected to win — the point is the pipeline
    and governance gates behave correctly, not that the TCN wins."""
    import os
    import tempfile

    import numpy as np

    from models.deep_sequence.dataset import SequenceDataset
    from models.deep_sequence.train import DeepSequenceConfig, train_deep_sequence_model
    from models.deep_sequence.artefact import (
        TrainedSequenceForecast,
        build_sequence_forecast_artefact,
    )
    from models.schemas import FeatureSpec

    rng = np.random.default_rng(0)
    n, w, f = 300, 12, 4
    X = rng.normal(size=(n, w, f)).astype("float32")
    y = (X[:, -1, 0] * 0.8 + rng.normal(scale=0.1, size=n)).astype("float32")
    ds = SequenceDataset(
        X=X, y=y, timestamps=np.arange(n),
        feature_names=["f0", "f1", "f2", "f3"], window=w, horizon=1,
    )
    res = train_deep_sequence_model(
        dataset=ds,
        config=DeepSequenceConfig(enabled=True, architecture="tcn"),
        holdout_fraction=0.25,
    )
    assert res.deep_model is not None
    assert res.comparison is not None  # harness actually ran
    # promote_eligible must equal the harness verdict — never hard-set.
    assert res.promote_eligible == bool(res.comparison.deep_beats_baseline)

    fs = [FeatureSpec(name=nm, dtype="float64") for nm in ds.feature_names]
    art = build_sequence_forecast_artefact(
        res, feature_specs=fs, target_kind="forward_return", horizon=1
    )
    assert art.metadata["deep_beats_baseline"] == bool(res.comparison.deep_beats_baseline)
    assert art.input_feature_means is not None
    assert art.input_feature_stds is not None
    assert art.target_mean is not None
    assert art.target_std is not None
    assert art.input_feature_means.shape == (f,)
    assert art.predict(X[:3]).shape == (3,)

    p = tempfile.mktemp(suffix=".pkl")
    try:
        art.save(p)
        if art.metadata["deep_beats_baseline"] is True:
            assert isinstance(TrainedSequenceForecast.load(p), TrainedSequenceForecast)
        else:
            with pytest.raises(ValueError, match="deep_beats_baseline is not True"):
                TrainedSequenceForecast.load(p)
    finally:
        if os.path.exists(p):
            os.unlink(p)


def test_sequence_window_alignment_is_contract_correct_and_safe() -> None:
    """The bridge builds an artefact-contract-aligned (window, n_feat) array
    from a recent-history dict (loop output): selects the artefact's feature
    columns in order, takes the trailing `window` rows, and returns None
    (caller skips safely) on any contract mismatch — never raises."""
    import numpy as np

    from signals.forecast_bridge import _align_sequence_to_artefact

    class _A:
        spec = {"window": 3, "n_features": 2}
        feature_specs = [
            type("S", (), {"name": "f0"})(),
            type("S", (), {"name": "f1"})(),
        ]

    hist = {
        "columns": ["f1", "x", "f0"],  # extra col + different order
        "rows": [[10, 9, 1], [20, 9, 2], [30, 9, 3], [40, 9, 4], [50, 9, 5]],
    }
    out = _align_sequence_to_artefact(hist, _A())
    assert out is not None and out.shape == (3, 2)
    assert out[-1].tolist() == [5.0, 50.0]  # [f0, f1] order, trailing window

    assert _align_sequence_to_artefact(np.zeros((3, 2)), _A()).shape == (3, 2)
    assert _align_sequence_to_artefact(np.zeros((4, 2)), _A()) is None  # wrong window
    assert _align_sequence_to_artefact({"columns": ["f0"], "rows": [[1]]}, _A()) is None
    assert _align_sequence_to_artefact("garbage", _A()) is None  # never raises


def test_attach_forecast_sequence_history_is_gated_zero_overhead() -> None:
    """The loop helper does NOTHING (no metadata key) unless a sequence
    member is enabled — the default state, so it's zero-overhead in prod."""
    import pandas as pd

    from system.trading_loop.helpers import attach_forecast_sequence_history

    class _C:
        metadata: dict = {}

    df = pd.DataFrame({"close": [1.0, 2.0, 3.0], "volume": [9, 9, 9]})
    c = _C()
    c.metadata = {}
    attach_forecast_sequence_history(c, df, enabled=False)
    assert "forecast_sequence_window" not in c.metadata  # gated off → no-op

    c2 = _C()
    c2.metadata = {}
    attach_forecast_sequence_history(c2, df, enabled=True)
    w = c2.metadata.get("forecast_sequence_window")
    assert w and sorted(w["columns"]) == ["close", "volume"] and len(w["rows"]) == 3


def test_forecast_bridge_disabled_is_inert() -> None:
    """Default config (disabled) → used=False, no crash. Phase B ships inert."""
    from signals.forecast_bridge import ForecastBridgeConfig, evaluate_features
    from models.schemas import Mode

    d = evaluate_features(
        features={"a": 1.0}, mode=Mode.PAPER, config=ForecastBridgeConfig()
    )
    assert d.used is False
    assert d.reason in ("disabled", "no_models")
