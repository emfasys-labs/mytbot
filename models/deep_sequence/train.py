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
            from models.deep_sequence.tcn import TCNSpec, build_tcn  # noqa: F401

            # Real TCN training is the operator's responsibility — this
            # build does not ship a torch-trained model. Surface a
            # clear note.
            note = (
                "tcn architecture requested but no torch trainer is shipped; "
                "promote_eligible will remain False"
            )
        except RuntimeError as exc:
            note = f"tcn unavailable: {exc}"
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
