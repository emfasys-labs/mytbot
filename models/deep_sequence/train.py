"""
models/deep_sequence/train.py
===============================
Wave 11 — unified training entry.

The operator points this at a ``SequenceDataset``. The harness:

  1. Always trains the ``RidgeSequenceBaseline`` — this is the floor.
  2. Optionally trains a deep model (TCN / TFT) when ``architecture``
     is set and PyTorch is available.
  3. Runs a held-out comparison via ``compare_against_baseline``.
  4. Returns a ``DeepTrainingResult`` whose ``promote_eligible`` flag
     is True only when the deep model wins per the configured rule.

The registry / governance contract is the operator's responsibility
post-training: ``promote_eligible=False`` should refuse to register
the deep model with ``approval_status >= paper``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import numpy as np

from models.deep_sequence.baseline import (
    RidgeSequenceBaseline,
    TrainedRidgeSequenceBaseline,
)
from models.deep_sequence.dataset import SequenceDataset
from models.deep_sequence.evaluate import (
    BaselineComparisonReport,
    compare_against_baseline,
)
from models.feature_contracts import compute_feature_hash
from models.schemas import FeatureSpec

logger = logging.getLogger(__name__)


# ── config ─────────────────────────────────────────────────────────────────


@dataclass
class DeepSequenceConfig:
    enabled: bool = False
    architecture: str = "none"  # "none" | "tcn" | "tft"
    baseline_alpha: float = 1.0

    # Comparison rule:
    #   deep wins if (mse_deep / mse_baseline) <= mse_ratio_threshold
    #   AND (deep_hit_rate - baseline_hit_rate) >= hit_rate_margin
    #   AND deep beats baseline on the cost-aware net P&L.
    mse_ratio_threshold: float = 0.95
    hit_rate_margin: float = 0.01
    round_trip_cost_bps: float = 5.0

    # Trainer controls. Conservative defaults preserve the existing harness
    # behaviour while letting governed panel runs choose a faster research pass.
    epochs: int = 80
    batch_size: int = 64
    patience: int = 10
    learning_rate: float = 1e-3
    seed: int = 17

    # Promotion: even when comparison wins, we never auto-promote past
    # ``research`` here. The operator must register manually.
    require_manual_promotion: bool = True


# ── result ─────────────────────────────────────────────────────────────────


@dataclass
class DeepTrainingResult:
    baseline: TrainedRidgeSequenceBaseline
    deep_model: Any = None
    comparison: Optional[BaselineComparisonReport] = None
    promote_eligible: bool = False
    feature_contract_hash: str = ""
    notes: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


# ── trainer ────────────────────────────────────────────────────────────────


def _train_tcn(
    *,
    X_train: np.ndarray,
    y_train: np.ndarray,
    X_test: np.ndarray,
    build_tcn,  # noqa: ANN001
    TCNSpec,  # noqa: ANN001,N803
    config: "DeepSequenceConfig",
    epochs: int = 80,
    batch_size: int = 64,
    patience: int = 10,
    lr: float = 1e-3,
    seed: int = 17,
):
    """Train a TCN with Adam + early stopping on an inner validation split.

    Returns ``(trained_eval_model, pred_deep_test_np, note)``. Pure-ish:
    deterministic seed, no global state, exceptions propagate to the caller
    which degrades to baseline-only. ``X`` shape ``(n, window, n_feat)``.
    """
    import torch
    from torch import nn

    torch.manual_seed(seed)
    np.random.seed(seed)

    X = np.asarray(X_train, dtype=np.float32)
    y_raw = np.asarray(y_train, dtype=np.float32).reshape(-1, 1)
    n, window, n_feat = X.shape
    x_mean = X.mean(axis=(0, 1), keepdims=True)
    x_std = X.std(axis=(0, 1), keepdims=True)
    x_std = np.where(x_std > 1e-8, x_std, 1.0).astype(np.float32)
    x_mean = x_mean.astype(np.float32)
    X = (X - x_mean) / x_std
    X_test_scaled = (np.asarray(X_test, dtype=np.float32) - x_mean) / x_std
    y_mean = y_raw.mean(axis=0, keepdims=True).astype(np.float32)
    y_std = y_raw.std(axis=0, keepdims=True).astype(np.float32)
    y_std = np.where(y_std > 1e-8, y_std, 1.0).astype(np.float32)
    y = (y_raw - y_mean) / y_std

    # Inner time-ordered validation split for early stopping (no shuffle —
    # preserves temporal order, avoids leakage).
    v_cut = max(1, int(n * 0.85))
    Xtr, Xva = torch.from_numpy(X[:v_cut]), torch.from_numpy(X[v_cut:])
    ytr, yva = torch.from_numpy(y[:v_cut]), torch.from_numpy(y[v_cut:])
    if len(Xva) == 0:  # tiny dataset → use train as its own val
        Xva, yva = Xtr, ytr

    model = build_tcn(
        TCNSpec(
            n_features=n_feat,
            window=window,
            channels=(32, 32, 32),
            kernel_size=3,
            dropout=0.1,
            output_dim=1,
        )
    )
    opt = torch.optim.Adam(model.parameters(), lr=lr, weight_decay=1e-5)
    loss_fn = nn.MSELoss()

    best_val = float("inf")
    best_state: dict | None = None
    bad = 0
    idx = np.arange(len(Xtr))
    for _ep in range(epochs):
        model.train()
        np.random.shuffle(idx)
        for s in range(0, len(idx), batch_size):
            b = idx[s : s + batch_size]
            opt.zero_grad()
            out = model(Xtr[b])
            loss = loss_fn(out, ytr[b])
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 5.0)
            opt.step()
        model.eval()
        with torch.no_grad():
            vloss = float(loss_fn(model(Xva), yva))
        if vloss < best_val - 1e-9:
            best_val = vloss
            best_state = {k: v.detach().clone() for k, v in model.state_dict().items()}
            bad = 0
        else:
            bad += 1
            if bad >= patience:
                break

    if best_state is not None:
        model.load_state_dict(best_state)
    # Persist the exact train-only scaler with the model. The artefact uses
    # it at inference so live/shadow scoring sees the same feature space.
    model.input_feature_means = x_mean.reshape(-1).copy()
    model.input_feature_stds = x_std.reshape(-1).copy()
    model.target_mean = y_mean.reshape(-1).copy()
    model.target_std = y_std.reshape(-1).copy()
    model.eval()
    with torch.no_grad():
        pred = model(torch.from_numpy(X_test_scaled))
    pred_np = pred.detach().cpu().numpy().reshape(-1)
    pred_np = pred_np * float(y_std.reshape(-1)[0]) + float(y_mean.reshape(-1)[0])
    note = (
        f"tcn trained: best_val_mse={best_val:.6g} "
        f"epochs_used={_ep + 1} n_feat={n_feat} window={window}"
    )
    return model, pred_np, note


def train_deep_sequence_model(
    *,
    dataset: SequenceDataset,
    config: DeepSequenceConfig,
    feature_specs: Optional[list[FeatureSpec]] = None,
    holdout_fraction: float = 0.2,
) -> DeepTrainingResult:
    """
    Train baseline + (optional) deep, run the comparison harness, and
    return the verdict.

    ``promote_eligible`` is True only when:
      - a deep model was actually trained,
      - the comparison report's ``deep_beats_baseline`` is True.

    The operator is still expected to manually flip the registry
    entry to ``paper`` after a soak window — see
    ``docs/MODEL_GOVERNANCE.md``.
    """
    if dataset.X.size == 0:
        raise ValueError("empty SequenceDataset")

    # Train/test split — last ``holdout_fraction`` is OOS.
    n = len(dataset.y)
    cutoff = int(n * (1.0 - holdout_fraction))
    cutoff = max(1, min(cutoff, n - 1))
    X_train, X_test = dataset.X[:cutoff], dataset.X[cutoff:]
    y_train, y_test = dataset.y[:cutoff], dataset.y[cutoff:]

    if len(X_train) < 5 or len(X_test) < 5:
        raise ValueError(
            f"insufficient rows for OOS comparison: train={len(X_train)} test={len(X_test)}"
        )

    # --- always train the baseline ---------------------------------
    base = RidgeSequenceBaseline(alpha=config.baseline_alpha)
    trained_baseline = base.fit(X_train, y_train)

    # Attach a feature contract — even though sequence "features" are
    # higher-dim than tabular ones, we still hash the underlying
    # column names for governance.
    fs = feature_specs or [
        FeatureSpec(name=name, dtype="float64") for name in (dataset.feature_names or ())
    ]
    if fs:
        trained_baseline.attach_feature_contract(fs)
    contract_hash = trained_baseline.feature_contract_hash or compute_feature_hash(fs) if fs else ""

    pred_baseline_test = np.asarray(trained_baseline.predict(X_test), dtype=float)

    # --- optional deep model ---------------------------------------
    deep_model: Any = None
    pred_deep_test: Optional[np.ndarray] = None
    note = ""

    arch = (config.architecture or "none").strip().lower()
    if arch in ("none", ""):
        note = "deep architecture disabled — only baseline trained"
    elif arch == "tcn":
        try:
            from models.deep_sequence.tcn import TCNSpec, build_tcn

            deep_model, pred_deep_test, note = _train_tcn(
                X_train=X_train, y_train=y_train, X_test=X_test,
                build_tcn=build_tcn,
                TCNSpec=TCNSpec,
                config=config,
                epochs=max(1, int(config.epochs)),
                batch_size=max(1, int(config.batch_size)),
                patience=max(1, int(config.patience)),
                lr=float(config.learning_rate),
                seed=int(config.seed),
            )
        except RuntimeError as exc:
            # torch absent → degrade safely to baseline-only (no crash).
            note = f"tcn unavailable: {exc}"
        except Exception as exc:  # noqa: BLE001
            note = f"tcn training failed: {exc.__class__.__name__}: {exc}"
            deep_model = None
            pred_deep_test = None
    elif arch == "tft":
        try:
            from models.deep_sequence.tft import TFTSpec, build_tft  # noqa: F401

            note = (
                "tft architecture requested but no torch trainer is shipped; "
                "promote_eligible will remain False"
            )
        except RuntimeError as exc:
            note = f"tft unavailable: {exc}"
    else:
        note = f"unknown architecture: {arch!r}"

    comparison: Optional[BaselineComparisonReport] = None
    if pred_deep_test is not None:
        comparison = compare_against_baseline(
            y_true=y_test,
            deep_predictions=pred_deep_test,
            baseline_predictions=pred_baseline_test,
            mse_ratio_threshold=config.mse_ratio_threshold,
            hit_rate_margin=config.hit_rate_margin,
            round_trip_cost_bps=config.round_trip_cost_bps,
        )

    promote_eligible = bool(comparison is not None and comparison.deep_beats_baseline)

    return DeepTrainingResult(
        baseline=trained_baseline,
        deep_model=deep_model,
        comparison=comparison,
        promote_eligible=promote_eligible,
        feature_contract_hash=contract_hash,
        notes=note,
        metadata={
            "n_train": len(X_train),
            "n_test": len(X_test),
            "architecture": arch,
        },
    )
