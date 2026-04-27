"""
models/deep_sequence/tcn.py
=============================
Wave 11 — Temporal Convolutional Network factory (torch-gated).

This module is intentionally a *stub*. Real TCN implementations
require PyTorch; we don't ship a deep-learning dependency by default.
``build_tcn(...)`` raises ``RuntimeError("torch required")`` when
PyTorch is not installed, so callers fail loudly with a clear message
instead of silently falling back to the baseline.

When the operator installs PyTorch, replace the stub body with a
real Bai-style TCN (dilated causal convolutions + residual blocks).
The training harness in ``models/deep_sequence/train.py`` is
already torch-aware and will exercise it.
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
class TCNSpec:
    n_features: int
    window: int
    channels: tuple[int, ...] = (32, 32, 32)
    kernel_size: int = 3
    dropout: float = 0.1
    output_dim: int = 1
    metadata: dict[str, Any] = field(default_factory=dict)


def torch_available() -> bool:
    return _TORCH_AVAILABLE


def build_tcn(spec: TCNSpec):
    """
    Construct a TCN module.

    When PyTorch is installed, replace this stub with a real TCN
    implementation. The training harness expects a ``torch.nn.Module``
    whose ``forward(x)`` accepts ``x`` of shape ``(batch, window,
    n_features)`` and returns ``(batch, output_dim)``.
    """
    if not _TORCH_AVAILABLE:
        raise RuntimeError(
            "torch is required for build_tcn — install PyTorch and replace the "
            "stub in models/deep_sequence/tcn.py with a real TCN implementation. "
            "The Wave 11 training harness will exercise it once present."
        )
    raise NotImplementedError(
        "PyTorch is installed but the TCN architecture has not been implemented in "
        "this build. The operator should plug in a Bai-style dilated-causal-conv TCN."
    )
