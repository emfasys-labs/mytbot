"""
models/deep_sequence/tcn.py
=============================
Wave 11 — Temporal Convolutional Network (Bai et al. 2018), torch-gated.

A real dilated-causal-convolution TCN: stacked residual ``TemporalBlock``s
with exponentially increasing dilation, weight-normed convs, ReLU + dropout,
and a 1×1 residual projection. Causality is enforced by left-padding then
chomping the extra right-hand timesteps, so output[t] only ever depends on
input[≤t] — no look-ahead leakage (critical for a forecasting model).

``forward(x)``: ``x`` shape ``(batch, window, n_features)`` →
``(batch, output_dim)`` (the last causal timestep through a linear head).

Torch is still gated: ``build_tcn`` raises a clear ``RuntimeError`` when
PyTorch is absent so callers degrade safely to the always-available
``RidgeSequenceBaseline`` rather than crashing. The TCN remains INERT until
the operator trains, validates (must beat baseline OOS after costs) and
registers/approves an artefact through the existing governance pipeline —
this module only provides the architecture.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

try:
    import torch
    from torch import nn

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


if _TORCH_AVAILABLE:

    class _Chomp1d(nn.Module):
        """Remove the ``chomp`` right-most timesteps added by left padding so
        the convolution is strictly causal (no future leakage)."""

        def __init__(self, chomp: int) -> None:
            super().__init__()
            self.chomp = int(chomp)

        def forward(self, x):  # noqa: ANN001
            return x[:, :, : -self.chomp].contiguous() if self.chomp > 0 else x

    class _TemporalBlock(nn.Module):
        def __init__(
            self,
            in_ch: int,
            out_ch: int,
            kernel_size: int,
            dilation: int,
            dropout: float,
        ) -> None:
            super().__init__()
            pad = (kernel_size - 1) * dilation  # left pad → causal after chomp

            def _wn_conv() -> nn.Module:
                return nn.utils.weight_norm(
                    nn.Conv1d(
                        in_ch if _wn_conv.first else out_ch,  # type: ignore[attr-defined]
                        out_ch,
                        kernel_size,
                        padding=pad,
                        dilation=dilation,
                    )
                )

            _wn_conv.first = True  # type: ignore[attr-defined]
            self.conv1 = _wn_conv()
            _wn_conv.first = False  # type: ignore[attr-defined]
            self.conv2 = _wn_conv()
            self.net = nn.Sequential(
                self.conv1, _Chomp1d(pad), nn.ReLU(), nn.Dropout(dropout),
                self.conv2, _Chomp1d(pad), nn.ReLU(), nn.Dropout(dropout),
            )
            self.downsample = (
                nn.Conv1d(in_ch, out_ch, 1) if in_ch != out_ch else None
            )
            self.relu = nn.ReLU()
            self._init_weights()

        def _init_weights(self) -> None:
            for m in (self.conv1, self.conv2):
                nn.init.kaiming_normal_(m.weight, nonlinearity="relu")
            if self.downsample is not None:
                nn.init.kaiming_normal_(self.downsample.weight, nonlinearity="relu")

        def forward(self, x):  # noqa: ANN001
            out = self.net(x)
            res = x if self.downsample is None else self.downsample(x)
            return self.relu(out + res)

    class TCN(nn.Module):
        """Bai-style TCN. Input ``(batch, window, n_features)``."""

        def __init__(self, spec: TCNSpec) -> None:
            super().__init__()
            self.spec = spec
            layers: list[nn.Module] = []
            prev = spec.n_features
            for i, ch in enumerate(spec.channels):
                layers.append(
                    _TemporalBlock(
                        prev, ch, spec.kernel_size,
                        dilation=2 ** i, dropout=spec.dropout,
                    )
                )
                prev = ch
            self.tcn = nn.Sequential(*layers)
            self.head = nn.Linear(prev, spec.output_dim)

        def forward(self, x):  # noqa: ANN001
            # (B, W, F) → (B, F, W) for conv1d.
            x = x.transpose(1, 2)
            y = self.tcn(x)              # (B, C, W)
            last = y[:, :, -1]           # causal: last timestep only
            return self.head(last)       # (B, output_dim)


def build_tcn(spec: TCNSpec):
    """Construct a torch TCN module from ``spec``.

    Raises a clear ``RuntimeError`` when torch is unavailable so the caller
    falls back to the Ridge baseline rather than crashing — preserving the
    Wave 11 safe-degradation contract.
    """
    if not _TORCH_AVAILABLE:
        raise RuntimeError(
            "torch is required for build_tcn — install PyTorch. With torch "
            "absent the deep-sequence path degrades to RidgeSequenceBaseline."
        )
    return TCN(spec)
