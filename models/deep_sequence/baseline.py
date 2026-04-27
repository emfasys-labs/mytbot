"""
models/deep_sequence/baseline.py
==================================
Wave 11 — sequence-model baseline.

Pure-NumPy ridge regression on flattened sequence windows. This is the
**always-available** reference model: every deep candidate must beat
it OOS after costs to be promoted past ``research`` status.

Why ridge on flattened windows? Two reasons:

1. It is dependency-free and deterministic; the comparison harness
   needs *something* to compare against in environments without torch.
2. It is the natural "tabular" lower bound — if a TCN/TFT cannot
   beat a ridge regression on flattened lookback windows, it is not
   adding value over the structured-ML approach already covered by
   Wave 6.
"""

from __future__ import annotations

import pickle
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from models.feature_contracts import compute_feature_hash
from models.schemas import FeatureSpec


@dataclass
class RidgeSequenceBaseline:
    alpha: float = 1.0

    def fit(
        self,
        X: np.ndarray,        # shape (n_samples, window, n_features)
        y: np.ndarray,        # shape (n_samples,)
    ) -> "TrainedRidgeSequenceBaseline":
        if X.ndim != 3:
            raise ValueError("X must be 3-D: (n_samples, window, n_features)")
        n, w, p = X.shape
        flat = X.reshape(n, w * p)
        mu = flat.mean(axis=0)
        sd = flat.std(axis=0)
        sd = np.where(sd > 1e-12, sd, 1.0)
        Xs = (flat - mu) / sd
        y = np.asarray(y, dtype=float).ravel()
        y_mean = float(y.mean())
        ytilde = y - y_mean
        gram = Xs.T @ Xs + float(self.alpha) * np.eye(Xs.shape[1])
        coef = np.linalg.solve(gram, Xs.T @ ytilde)
        return TrainedRidgeSequenceBaseline(
            alpha=self.alpha,
            window=w,
            n_features=p,
            coef=coef,
            intercept=y_mean,
            feature_means=mu,
            feature_stds=sd,
        )


@dataclass
class TrainedRidgeSequenceBaseline:
    alpha: float
    window: int
    n_features: int
    coef: np.ndarray
    intercept: float
    feature_means: np.ndarray
    feature_stds: np.ndarray
    feature_specs: list[FeatureSpec] = field(default_factory=list)
    feature_contract_hash: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def predict(self, X: np.ndarray) -> np.ndarray:
        if X.ndim != 3:
            raise ValueError("X must be 3-D: (n_samples, window, n_features)")
        n, w, p = X.shape
        if (w, p) != (self.window, self.n_features):
            raise ValueError(
                f"shape mismatch: got window={w} n_features={p}, "
                f"expected window={self.window} n_features={self.n_features}"
            )
        flat = X.reshape(n, w * p)
        Xs = (flat - self.feature_means) / self.feature_stds
        return Xs @ self.coef + self.intercept

    def attach_feature_contract(self, feature_specs: list[FeatureSpec]) -> None:
        self.feature_specs = list(feature_specs)
        self.feature_contract_hash = compute_feature_hash(feature_specs)

    def save(self, path: Path | str) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "wb") as f:
            pickle.dump(self, f, protocol=pickle.HIGHEST_PROTOCOL)

    @staticmethod
    def load(path: Path | str) -> "TrainedRidgeSequenceBaseline":
        with open(path, "rb") as f:
            obj = pickle.load(f)
        if not isinstance(obj, TrainedRidgeSequenceBaseline):
            raise TypeError(f"file does not contain TrainedRidgeSequenceBaseline: {type(obj).__name__}")
        if obj.feature_specs:
            expected = compute_feature_hash(obj.feature_specs)
            if expected != obj.feature_contract_hash:
                raise ValueError("baseline feature_contract_hash mismatch — refusing to load")
        return obj
