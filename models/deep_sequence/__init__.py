"""
models/deep_sequence/
=======================
Wave 11 — deep sequence models, strictly gated.

Public surface:

- ``make_sequence_windows`` — leakage-safe windowing.
- ``RidgeSequenceBaseline`` — always-available NumPy baseline that
  every deep candidate must beat OOS after costs.
- ``BaselineComparisonReport`` + ``compare_against_baseline`` — the
  comparison harness. The ``deep_beats_baseline`` verdict is the
  governance gate: deep models do not get promoted past ``research``
  status until the report says they win.
- ``build_tcn`` / ``build_tft`` — torch-gated factories. Raise
  ``RuntimeError("torch required")`` when PyTorch is not installed.
- ``train_deep_sequence_model`` — unified trainer.
- ``score_sequence`` — runtime helper.

Wave-11 rules (encoded in code, not just docs):

1. Deep models must compete against tabular / linear baselines.
2. They must beat the baseline OOS *after* execution costs.
3. They are ``enabled: false`` by default.
4. They use the same registry / governance pipeline as Wave 1.
5. They never directly generate orders.
"""

from models.deep_sequence.baseline import (
    RidgeSequenceBaseline,
    TrainedRidgeSequenceBaseline,
)
from models.deep_sequence.dataset import (
    SequenceDataset,
    make_sequence_windows,
)
from models.deep_sequence.evaluate import (
    BaselineComparisonReport,
    compare_against_baseline,
)
from models.deep_sequence.infer import (
    DeepSequenceForecastResult,
    score_sequence,
)
from models.deep_sequence.train import (
    DeepSequenceConfig,
    DeepTrainingResult,
    train_deep_sequence_model,
)

__all__ = [
    "BaselineComparisonReport",
    "DeepSequenceConfig",
    "DeepSequenceForecastResult",
    "DeepTrainingResult",
    "RidgeSequenceBaseline",
    "SequenceDataset",
    "TrainedRidgeSequenceBaseline",
    "compare_against_baseline",
    "make_sequence_windows",
    "score_sequence",
    "train_deep_sequence_model",
]


# torch-gated factories (re-exported lazily so the package import does
# not require torch).

def build_tcn(*args, **kwargs):
    """Lazy import; raises if torch is unavailable."""
    from models.deep_sequence.tcn import build_tcn as _build

    return _build(*args, **kwargs)


def build_tft(*args, **kwargs):
    """Lazy import; raises if torch is unavailable."""
    from models.deep_sequence.tft import build_tft as _build

    return _build(*args, **kwargs)


__all__ += ["build_tcn", "build_tft"]
