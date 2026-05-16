"""Phase C regime-transition detector.

Binary, governed, shadow-first model: given current cross-section regime
features, estimate the probability that the next horizon enters a stress /
volatility-expansion transition. This never changes allocation by itself.
"""

from __future__ import annotations

import pickle
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np


@dataclass
class RegimeTransitionPrediction:
    probability: float
    label: str
    threshold: float
    model_version: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class RegimeTransitionDetector:
    feature_names: tuple[str, ...]
    threshold: float = 0.6
    model_version: str = "research"
    feature_means: np.ndarray | None = None
    feature_stds: np.ndarray | None = None
    coef: np.ndarray | None = None
    intercept: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)

    def _standardise(self, X: np.ndarray) -> np.ndarray:
        arr = np.asarray(X, dtype=float)
        if arr.ndim == 1:
            arr = arr.reshape(1, -1)
        if self.feature_means is None or self.feature_stds is None:
            return arr
        sd = np.where(np.asarray(self.feature_stds) > 1e-12, self.feature_stds, 1.0)
        return (arr - self.feature_means) / sd

    def fit(self, X: np.ndarray, y: np.ndarray, *, l2: float = 1.0) -> "RegimeTransitionDetector":
        """Fit a deterministic logistic model.

        Uses sklearn when available and a small NumPy gradient-descent fallback
        otherwise. The fitted artefact stores only simple arrays.
        """
        X = np.asarray(X, dtype=float)
        y = np.asarray(y, dtype=float).reshape(-1)
        if X.ndim != 2:
            raise ValueError("X must be 2-D")
        if len(X) != len(y) or len(y) < 20:
            raise ValueError("insufficient rows for transition detector")
        self.feature_means = X.mean(axis=0)
        self.feature_stds = X.std(axis=0, ddof=1)
        Xz = self._standardise(X)
        try:
            from sklearn.linear_model import LogisticRegression

            clf = LogisticRegression(
                C=max(1e-6, 1.0 / max(float(l2), 1e-6)),
                class_weight="balanced",
                max_iter=1000,
                solver="lbfgs",
                random_state=17,
            )
            clf.fit(Xz, y.astype(int))
            self.coef = np.asarray(clf.coef_[0], dtype=float)
            self.intercept = float(clf.intercept_[0])
            self.metadata["backend"] = "sklearn_logistic"
        except Exception:  # noqa: BLE001
            w = np.zeros(Xz.shape[1], dtype=float)
            b = 0.0
            lr = 0.05
            lam = float(l2)
            for _ in range(1200):
                z = Xz @ w + b
                p = 1.0 / (1.0 + np.exp(-np.clip(z, -40, 40)))
                err = p - y
                w -= lr * ((Xz.T @ err) / len(y) + lam * w / len(y))
                b -= lr * float(err.mean())
            self.coef = w
            self.intercept = b
            self.metadata["backend"] = "numpy_logistic"
        return self

    def predict_probability(self, x: np.ndarray | list[float]) -> float:
        if self.coef is None:
            return 0.0
        arr = np.asarray(x, dtype=float)
        if arr.ndim == 1 and len(arr) != len(self.feature_names):
            return 0.0
        Xz = self._standardise(arr)
        z = Xz @ self.coef + self.intercept
        p = 1.0 / (1.0 + np.exp(-np.clip(z, -40, 40)))
        return float(np.asarray(p).reshape(-1)[0])

    def predict(self, x: np.ndarray | list[float]) -> RegimeTransitionPrediction:
        p = self.predict_probability(x)
        return RegimeTransitionPrediction(
            probability=p,
            label="stress_transition" if p >= self.threshold else "normal",
            threshold=float(self.threshold),
            model_version=self.model_version,
            metadata=dict(self.metadata or {}),
        )

    def save(self, path: str | Path) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("wb") as f:
            pickle.dump(self, f, protocol=pickle.HIGHEST_PROTOCOL)

    @staticmethod
    def load(path: str | Path) -> "RegimeTransitionDetector":
        with Path(path).open("rb") as f:
            obj = pickle.load(f)
        if not isinstance(obj, RegimeTransitionDetector):
            raise TypeError(f"not a RegimeTransitionDetector: {type(obj).__name__}")
        if obj.coef is None:
            raise ValueError("transition detector is unfitted")
        return obj
