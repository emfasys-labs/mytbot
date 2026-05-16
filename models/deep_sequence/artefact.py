"""
models/deep_sequence/artefact.py
==================================
Wave 11 / Phase B — packaged, governed sequence-forecast artefact.

Wraps a trained TCN as a portable, picklable artefact that conforms to
the inference contract used by ``infer.score_sequence`` (``.predict(X3d)``,
``.feature_contract_hash``) and the forecast-bridge member contract
(``.feature_specs``, ``.target_kind``, ``.horizon``, ``.metadata``).

Defence in depth (matches the forecast_bridge safety gate): a deep
artefact will **refuse to load** unless its metadata carries
``deep_beats_baseline is True`` — i.e. it passed the OOS, cost-aware
comparison harness. Config alone can never resurrect an unvalidated model.

Torch is gated: ``predict`` raises a clear error when torch is absent;
``score_sequence`` already turns that into ``used=False`` so the live
system degrades safely to the Ridge baseline.
"""

from __future__ import annotations

import pickle
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from models.feature_contracts import compute_feature_hash
from models.schemas import FeatureSpec

MODEL_KIND = "tcn_sequence"


@dataclass
class TrainedSequenceForecast:
    spec: dict[str, Any]                 # TCNSpec kwargs (rebuildable)
    state_np: dict[str, np.ndarray]      # torch state_dict as CPU numpy
    target_kind: str = "forward_return"
    horizon: int = 1
    feature_specs: list[FeatureSpec] = field(default_factory=list)
    feature_contract_hash: str = ""
    input_feature_means: np.ndarray | None = None
    input_feature_stds: np.ndarray | None = None
    target_mean: np.ndarray | None = None
    target_std: np.ndarray | None = None
    model_kind: str = MODEL_KIND
    metadata: dict[str, Any] = field(default_factory=dict)

    # ── inference ────────────────────────────────────────────────────
    def predict(self, X: np.ndarray) -> np.ndarray:
        """X: (n, window, n_features) → (n,) predictions. Torch-gated."""
        from models.deep_sequence.tcn import TCNSpec, build_tcn  # raises if no torch
        import torch

        arr = np.asarray(X, dtype=np.float32)
        if arr.ndim == 2:
            arr = arr.reshape(1, *arr.shape)
        if arr.ndim != 3:
            raise ValueError("X must be (n, window, n_features)")
        if self.input_feature_means is not None and self.input_feature_stds is not None:
            mu = np.asarray(self.input_feature_means, dtype=np.float32).reshape(1, 1, -1)
            sd = np.asarray(self.input_feature_stds, dtype=np.float32).reshape(1, 1, -1)
            if arr.shape[-1] != mu.shape[-1]:
                raise ValueError(
                    f"feature scaler mismatch: got {arr.shape[-1]} features, expected {mu.shape[-1]}"
                )
            sd = np.where(sd > 1e-8, sd, 1.0)
            arr = (arr - mu) / sd
        model = build_tcn(TCNSpec(**self.spec))
        sd = {k: torch.from_numpy(np.asarray(v)) for k, v in self.state_np.items()}
        model.load_state_dict(sd)
        model.eval()
        with torch.no_grad():
            out = model(torch.from_numpy(arr))
        pred = out.detach().cpu().numpy().reshape(-1)
        if self.target_mean is not None and self.target_std is not None:
            mu_y = np.asarray(self.target_mean, dtype=np.float32).reshape(-1)
            sd_y = np.asarray(self.target_std, dtype=np.float32).reshape(-1)
            scale = float(sd_y[0]) if len(sd_y) and float(sd_y[0]) > 1e-8 else 1.0
            shift = float(mu_y[0]) if len(mu_y) else 0.0
            pred = pred * scale + shift
        return pred

    def attach_feature_contract(self, feature_specs: list[FeatureSpec]) -> None:
        self.feature_specs = list(feature_specs)
        self.feature_contract_hash = compute_feature_hash(feature_specs)

    # ── persistence (governed) ───────────────────────────────────────
    def save(self, path: Path | str) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "wb") as f:
            pickle.dump(self, f, protocol=pickle.HIGHEST_PROTOCOL)

    @staticmethod
    def load(path: Path | str) -> "TrainedSequenceForecast":
        with open(path, "rb") as f:
            obj = pickle.load(f)
        if not isinstance(obj, TrainedSequenceForecast):
            raise TypeError(
                f"file does not contain TrainedSequenceForecast: {type(obj).__name__}"
            )
        if obj.feature_specs:
            if compute_feature_hash(obj.feature_specs) != obj.feature_contract_hash:
                raise ValueError("sequence feature_contract_hash mismatch — refusing to load")
        # Defence in depth: never load a deep artefact that did not pass the
        # OOS cost-aware baseline comparison.
        if obj.metadata.get("deep_beats_baseline") is not True:
            raise ValueError(
                "TrainedSequenceForecast.deep_beats_baseline is not True — "
                "model did not beat the baseline OOS after costs; refusing to load"
            )
        return obj


def build_sequence_forecast_artefact(
    result: Any,                       # DeepTrainingResult
    *,
    feature_specs: list[FeatureSpec],
    target_kind: str = "forward_return",
    horizon: int = 1,
) -> TrainedSequenceForecast:
    """Package a ``DeepTrainingResult`` into a governed artefact.

    ``deep_beats_baseline`` is taken HONESTLY from the comparison report —
    never hard-set. If the deep model did not win OOS after costs the
    artefact is still built (for inspection) but will fail ``.load`` and
    the forecast-bridge gate, so it can never influence decisions.
    """
    import torch  # noqa: F401  (artefact only valid where torch trained it)

    model = result.deep_model
    if model is None:
        raise ValueError("DeepTrainingResult has no deep_model to package")
    state_np = {k: v.detach().cpu().numpy() for k, v in model.state_dict().items()}
    spec = dict(getattr(model, "spec").__dict__) if hasattr(model, "spec") else {}
    # TCNSpec.metadata is not a constructor arg we want to round-trip blindly.
    spec.pop("metadata", None)

    cmp_ = result.comparison
    beats = bool(cmp_ is not None and cmp_.deep_beats_baseline)
    art = TrainedSequenceForecast(
        spec=spec,
        state_np=state_np,
        target_kind=target_kind,
        horizon=int(horizon),
        input_feature_means=(
            np.asarray(getattr(model, "input_feature_means"), dtype=np.float32)
            if hasattr(model, "input_feature_means")
            else None
        ),
        input_feature_stds=(
            np.asarray(getattr(model, "input_feature_stds"), dtype=np.float32)
            if hasattr(model, "input_feature_stds")
            else None
        ),
        target_mean=(
            np.asarray(getattr(model, "target_mean"), dtype=np.float32)
            if hasattr(model, "target_mean")
            else None
        ),
        target_std=(
            np.asarray(getattr(model, "target_std"), dtype=np.float32)
            if hasattr(model, "target_std")
            else None
        ),
        model_kind=MODEL_KIND,
        metadata={
            "deep_beats_baseline": beats,            # honest, from harness
            "promote_eligible": bool(result.promote_eligible),
            "notes": result.notes,
            "comparison": (
                {
                    "n_oos": cmp_.n_oos,
                    "mse_ratio": cmp_.mse_ratio,
                    "hit_rate_deep": cmp_.hit_rate_deep,
                    "hit_rate_baseline": cmp_.hit_rate_baseline,
                    "net_pnl_deep": cmp_.net_pnl_deep,
                    "net_pnl_baseline": cmp_.net_pnl_baseline,
                    "failures": list(cmp_.failures),
                }
                if cmp_ is not None
                else None
            ),
        },
    )
    if feature_specs:
        art.attach_feature_contract(feature_specs)
    return art
