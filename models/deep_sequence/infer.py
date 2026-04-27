"""
models/deep_sequence/infer.py
===============================
Wave 11 — runtime inference helper for sequence models.

Mirrors the pattern in ``signals/forecast_bridge.py``: by default the
runtime returns a "no model" decision so the live system continues
unchanged. When an operator has registered an approved deep model
*and* validated its comparison report, the decision carries a
forecast.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

import numpy as np


@dataclass
class DeepSequenceForecastResult:
    used: bool
    reason: str            # "ok" | "disabled" | "no_artefact" | "wrong_shape" | "predict_failed"
    prediction: Optional[float] = None
    architecture: Optional[str] = None
    feature_hash: Optional[str] = None
    metadata: dict = field(default_factory=dict)


def score_sequence(
    *,
    artefact: Any,                # TrainedRidgeSequenceBaseline | torch model | None
    sequence: np.ndarray,         # shape (window, n_features) or (1, window, n_features)
    architecture: str = "baseline",
) -> DeepSequenceForecastResult:
    if artefact is None:
        return DeepSequenceForecastResult(used=False, reason="no_artefact")

    seq = np.asarray(sequence, dtype=float)
    if seq.ndim == 2:
        seq = seq.reshape(1, *seq.shape)
    if seq.ndim != 3:
        return DeepSequenceForecastResult(used=False, reason="wrong_shape")

    try:
        out = np.asarray(artefact.predict(seq), dtype=float).ravel()
    except Exception as exc:  # noqa: BLE001
        return DeepSequenceForecastResult(
            used=False,
            reason="predict_failed",
            metadata={"error": str(exc)},
        )

    fh = getattr(artefact, "feature_contract_hash", None)
    return DeepSequenceForecastResult(
        used=True,
        reason="ok",
        prediction=float(out[0]),
        architecture=architecture,
        feature_hash=fh if isinstance(fh, str) and fh else None,
    )
