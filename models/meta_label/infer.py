"""
models/meta_label/infer.py
===========================
Wave 2 — runtime inference helpers for the trained meta-labeller.

This module is intentionally thin. ``signals/trained_meta_labeler.py``
is the call-site adapter that knows about ``SignalCandidate`` /
``Opportunity`` and the strategy config; here we just expose:

- ``score_features(artefact, X)`` — return probability per row.
- ``MetaLabelDecision`` — a tiny dataclass the strategy/runtime hands
  to the dashboard so the funnel ("strategy → meta → risk → exec")
  can show the probability and the threshold for every candidate.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd

from models.meta_label.train import TrainedMetaLabel


@dataclass
class MetaLabelDecision:
    """One decision row, in the form the dashboard expects."""

    kept: bool
    probability: Optional[float]
    threshold: float
    reason: str  # "approved" | "below_threshold" | "no_model_passthrough" | "error"
    model_name: Optional[str] = None
    model_version: Optional[str] = None
    feature_hash: Optional[str] = None
    metadata: dict[str, object] = field(default_factory=dict)


def score_features(
    artefact: TrainedMetaLabel,
    X: pd.DataFrame | np.ndarray,
) -> np.ndarray:
    """
    Return calibrated P(meta-label = 1) per row.

    ``X`` may be a DataFrame (will be reordered to the artefact's feature
    ordering) or a NumPy array (must already match the ordering).
    """
    return np.asarray(artefact.predict_proba(X), dtype=float).ravel()
