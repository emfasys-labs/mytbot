"""
models/meta_label/
==================
Wave 2 — trained meta-labelling.

Public surface (kept narrow on purpose):

- ``MetaLabelDataset`` and ``build_dataset_from_close`` — construct
  feature/label matrices using triple-barrier outcomes (no leakage).
- ``TrainedMetaLabel`` — fitted artefact with feature contract,
  calibrator, and raw classifier. Pickleable.
- ``train_meta_label_model`` — main entry point; returns a
  ``TrainedMetaLabel`` plus a ``MetaLabelEvalReport``.
- ``threshold_for`` / ``ThresholdConfig`` — per-mode/regime probability
  thresholds with safe defaults.

The runtime call site lives in ``signals/trained_meta_labeler.py`` —
keep this package import-light (numpy + pandas only; sklearn optional).
"""

from models.meta_label.dataset import (
    MetaLabelDataset,
    build_dataset_from_close,
    enforce_no_future_leakage,
)
from models.meta_label.thresholds import (
    DEFAULT_PROB_THRESHOLD,
    ThresholdConfig,
    threshold_for,
)
from models.meta_label.train import (
    MetaLabelEvalReport,
    TrainedMetaLabel,
    train_meta_label_model,
)
from models.meta_label.infer import (
    MetaLabelDecision,
    score_features,
)

__all__ = [
    "DEFAULT_PROB_THRESHOLD",
    "MetaLabelDataset",
    "MetaLabelDecision",
    "MetaLabelEvalReport",
    "ThresholdConfig",
    "TrainedMetaLabel",
    "build_dataset_from_close",
    "enforce_no_future_leakage",
    "score_features",
    "threshold_for",
    "train_meta_label_model",
]
