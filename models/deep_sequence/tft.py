"""
models/deep_sequence/tft.py
=============================
Wave 11 — Temporal Fusion Transformer factory (torch-gated).

Stub — same pattern as ``tcn.py``. The TFT (Lim et al., 2019) requires
PyTorch and is left unimplemented in this build. The operator with a
deep-learning environment should plug in a real TFT implementation
(e.g. via ``pytorch-forecasting``) and the rest of the Wave 11
machinery — dataset, baseline, comparison harness — will work
unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

try:
    import torch  # type: ignore  # noqa: F401

    _TORCH_AVAILABLE = True
except Exception:  # noqa: BLE001
    _TORCH_AVAILABLE = False


@dataclass
class TFTSpec:
    n_features: int
    window: int
    hidden_size: int = 64
    attention_heads: int = 4
    dropout: float = 0.1
    output_dim: int = 1
    metadata: dict[str, Any] = field(default_factory=dict)


def torch_available() -> bool:
    return _TORCH_AVAILABLE


def build_tft(spec: TFTSpec):
    if not _TORCH_AVAILABLE:
        raise RuntimeError(
            "torch is required for build_tft — install PyTorch and replace the "
            "stub in models/deep_sequence/tft.py with a real TFT implementation. "
            "Recommended: wrap pytorch-forecasting's TemporalFusionTransformer."
        )
    raise NotImplementedError(
        "PyTorch is installed but the TFT architecture has not been implemented in "
        "this build."
    )
