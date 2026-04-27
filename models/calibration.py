"""
models/calibration.py
======================
Wave 1 — thin calibration helpers.

Calibration is part of the model contract. A classifier that emits
uncalibrated probabilities cannot be used for sizing without producing
biased expected values. Wave 2 (trained meta-labelling) is the first
consumer; Wave 6 (forecast-native ML) is the second.

This module deliberately implements *minimal* isotonic and Platt
calibrators inline (NumPy only) so the package can be imported without
sklearn. If sklearn is available we delegate to its calibrators —
they are better tested and faster — but the no-sklearn fallback is the
contract floor so unit tests and lightweight environments work.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np

try:  # Optional: sklearn provides better-tested implementations.
    from sklearn.isotonic import IsotonicRegression  # type: ignore
    from sklearn.linear_model import LogisticRegression  # type: ignore

    _SKLEARN_AVAILABLE = True
except Exception:  # noqa: BLE001
    IsotonicRegression = None  # type: ignore
    LogisticRegression = None  # type: ignore
    _SKLEARN_AVAILABLE = False


@dataclass
class IdentityCalibrator:
    """No calibration — useful when a model emits already-calibrated output."""

    method: str = "none"

    def fit(self, scores: np.ndarray, labels: np.ndarray) -> "IdentityCalibrator":
        return self

    def transform(self, scores: np.ndarray) -> np.ndarray:
        return np.clip(np.asarray(scores, dtype=float), 0.0, 1.0)


@dataclass
class IsotonicCalibrator:
    """Isotonic calibration. Falls back to a NumPy PAV implementation."""

    method: str = "isotonic"
    _model: Optional[object] = None
    _fallback_x: Optional[np.ndarray] = None
    _fallback_y: Optional[np.ndarray] = None

    def fit(self, scores: np.ndarray, labels: np.ndarray) -> "IsotonicCalibrator":
        s = np.asarray(scores, dtype=float).ravel()
        y = np.asarray(labels, dtype=float).ravel()
        if s.shape != y.shape:
            raise ValueError("scores and labels must have the same shape")
        if _SKLEARN_AVAILABLE:
            self._model = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0)
            self._model.fit(s, y)
            return self
        # Fallback: pool-adjacent-violators on (s sorted, y mean per bucket).
        order = np.argsort(s, kind="mergesort")
        xs = s[order]
        ys = y[order]
        # PAV
        n = len(ys)
        weights = np.ones(n)
        values = ys.astype(float).copy()
        i = 0
        while i < len(values) - 1:
            if values[i] > values[i + 1]:
                w = weights[i] + weights[i + 1]
                merged = (values[i] * weights[i] + values[i + 1] * weights[i + 1]) / w
                values[i] = merged
                weights[i] = w
                values = np.delete(values, i + 1)
                weights = np.delete(weights, i + 1)
                xs = np.delete(xs, i + 1)
                if i > 0:
                    i -= 1
            else:
                i += 1
        self._fallback_x = xs
        self._fallback_y = np.clip(values, 0.0, 1.0)
        return self

    def transform(self, scores: np.ndarray) -> np.ndarray:
        s = np.asarray(scores, dtype=float).ravel()
        if _SKLEARN_AVAILABLE and self._model is not None:
            return np.clip(self._model.predict(s), 0.0, 1.0)
        if self._fallback_x is None or self._fallback_y is None:
            raise RuntimeError("IsotonicCalibrator must be fit() before transform()")
        return np.clip(np.interp(s, self._fallback_x, self._fallback_y), 0.0, 1.0)


@dataclass
class PlattCalibrator:
    """Platt scaling — logistic regression on raw scores."""

    method: str = "platt"
    _a: float = 0.0
    _b: float = 0.0

    def fit(self, scores: np.ndarray, labels: np.ndarray) -> "PlattCalibrator":
        s = np.asarray(scores, dtype=float).ravel()
        y = np.asarray(labels, dtype=float).ravel()
        if _SKLEARN_AVAILABLE:
            lr = LogisticRegression(solver="lbfgs")
            lr.fit(s.reshape(-1, 1), y.astype(int))
            self._a = float(lr.coef_[0, 0])
            self._b = float(lr.intercept_[0])
            return self
        # Closed-form gradient descent fallback (small Newton steps).
        a, b = 0.0, 0.0
        for _ in range(200):
            z = a * s + b
            p = 1.0 / (1.0 + np.exp(-z))
            err = p - y
            ga = float(np.mean(err * s))
            gb = float(np.mean(err))
            a -= 0.5 * ga
            b -= 0.5 * gb
        self._a = a
        self._b = b
        return self

    def transform(self, scores: np.ndarray) -> np.ndarray:
        s = np.asarray(scores, dtype=float).ravel()
        z = self._a * s + self._b
        return np.clip(1.0 / (1.0 + np.exp(-z)), 0.0, 1.0)


def make_calibrator(method: str):
    """Factory: ``"none"`` | ``"isotonic"`` | ``"platt"``."""
    m = (method or "none").strip().lower()
    if m in ("", "none", "identity"):
        return IdentityCalibrator()
    if m == "isotonic":
        return IsotonicCalibrator()
    if m == "platt":
        return PlattCalibrator()
    raise ValueError(f"unknown calibration method: {method!r}")
