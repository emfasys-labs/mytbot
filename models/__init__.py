"""
models/
=======
Wave 1: model registry, prediction storage, feature contracts, calibration.

This package is governance plumbing only. It does not train, infer, or
execute. Trained-model implementations live in ``models/<family>/`` (e.g.
``models/meta_label/`` from Wave 2 onwards), and they MUST register through
``models.registry`` and write predictions through
``models.prediction_store`` to be allowed in live mode.

See ``docs/MODEL_GOVERNANCE.md`` for the full contract.
"""

from models.schemas import (
    ApprovalStatus,
    FeatureContract,
    FeatureSpec,
    Mode,
    ModelContract,
    Prediction,
    Task,
    TrainingDatasetSpec,
    canonical_feature_list,
)
from models.feature_contracts import (
    compute_feature_hash,
    require_as_of_safe,
)

__all__ = [
    "ApprovalStatus",
    "FeatureContract",
    "FeatureSpec",
    "Mode",
    "ModelContract",
    "Prediction",
    "Task",
    "TrainingDatasetSpec",
    "canonical_feature_list",
    "compute_feature_hash",
    "require_as_of_safe",
]
