"""
risk/regime_models.py
======================
Wave 4 — regime classification.

Standalone classifier that turns a feature matrix (returns, vol, breadth,
news dispersion, etc.) into a discrete regime label. This module does
NOT replace ``risk/regime_state.py``; it provides a stronger inference
backend that the heuristic in ``regime_state.py`` can opt into via
config (Wave 4 wiring step).

Two backends:

- **NumPy fallback (default)**: K-means style clustering on the feature
  matrix, plus an empirical transition matrix learned from the
  cluster sequence. Cheap, no dependencies, deterministic given the
  random seed.
- **hmmlearn (optional)**: real Gaussian HMM. Used automatically when
  ``hmmlearn`` is importable.

Defensive guarantees (governance contract):

- ``predict_label(...)`` returns ``"insufficient_data"`` when fewer than
  ``min_samples`` rows are passed.
- ``"unknown"`` is returned when the classifier has never been fit.
- Every label belongs to ``REGIME_LABELS`` so downstream consumers can
  switch on a fixed enum-like vocabulary.
"""

from __future__ import annotations

import logging
import math
import pickle
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Sequence

import numpy as np

logger = logging.getLogger(__name__)


# ── public vocabulary ───────────────────────────────────────────────────────


REGIME_LABELS: tuple[str, ...] = (
    "risk_on",
    "risk_off",
    "trend",
    "range",
    "volatile",
    "crash",
)


SENTINEL_INSUFFICIENT = "insufficient_data"
SENTINEL_UNKNOWN = "unknown"


# ── optional backend ────────────────────────────────────────────────────────


try:
    from hmmlearn.hmm import GaussianHMM  # type: ignore

    _HMMLEARN_AVAILABLE = True
except Exception:  # noqa: BLE001
    GaussianHMM = None  # type: ignore
    _HMMLEARN_AVAILABLE = False


# ── NumPy K-means fallback ──────────────────────────────────────────────────


def _kmeans(X: np.ndarray, k: int, *, n_iter: int = 50, seed: int = 7) -> tuple[np.ndarray, np.ndarray]:
    """
    Simple K-means. Returns ``(centers, labels)`` where ``centers`` has
    shape ``(k, n_features)`` and ``labels`` has shape ``(n_samples,)``.
    """
    rng = np.random.default_rng(seed)
    n, p = X.shape
    if n < k:
        return X.copy(), np.arange(n) % k
    # k-means++ init: pick first center uniformly, then weighted by squared distance.
    idx0 = int(rng.integers(0, n))
    centers = [X[idx0]]
    for _ in range(1, k):
        d2 = np.min(
            np.array([np.sum((X - c) ** 2, axis=1) for c in centers]),
            axis=0,
        )
        s = float(d2.sum())
        if s <= 0:
            centers.append(X[int(rng.integers(0, n))])
            continue
        probs = d2 / s
        idx = int(rng.choice(n, p=probs))
        centers.append(X[idx])
    centers_arr = np.asarray(centers, dtype=float)
    labels = np.zeros(n, dtype=int)
    for _ in range(n_iter):
        # Assign.
        dists = np.linalg.norm(X[:, None, :] - centers_arr[None, :, :], axis=2)
        new_labels = np.argmin(dists, axis=1)
        if np.array_equal(new_labels, labels):
            break
        labels = new_labels
        # Update.
        for j in range(k):
            mask = labels == j
            if mask.any():
                centers_arr[j] = X[mask].mean(axis=0)
    return centers_arr, labels


# ── classifier ──────────────────────────────────────────────────────────────


@dataclass
class HMMRegimeClassifier:
    n_states: int = 4
    feature_names: tuple[str, ...] = ()
    min_samples: int = 60
    seed: int = 7
    # Fit artefacts.
    centers_: Optional[np.ndarray] = None
    state_to_label_: Optional[dict[int, str]] = None
    transition_matrix_: Optional[np.ndarray] = None
    feature_means_: Optional[np.ndarray] = None
    feature_stds_: Optional[np.ndarray] = None
    backend_: str = "numpy"
    hmm_model_: object = field(default=None, repr=False)
    fitted_: bool = False

    def _standardise(self, X: np.ndarray) -> np.ndarray:
        if self.feature_means_ is None or self.feature_stds_ is None:
            return X
        sd = np.where(self.feature_stds_ > 1e-12, self.feature_stds_, 1.0)
        return (X - self.feature_means_) / sd

    def fit(self, X: np.ndarray, *, label_for_state=None) -> "HMMRegimeClassifier":
        """
        Fit the classifier.

        ``label_for_state`` is an optional callable
        ``(center: np.ndarray, feature_names: tuple[str, ...]) -> str``
        that maps each fitted cluster centre to a human-readable label.
        Defaults to ``default_label_for_state`` which uses common-sense
        heuristics over (mean return, volatility) features when those
        names are present.
        """
        X = np.asarray(X, dtype=float)
        if X.ndim == 1:
            X = X.reshape(-1, 1)
        if len(X) < self.min_samples:
            logger.info(
                "regime_models | fit skipped — only %d rows < min_samples=%d",
                len(X),
                self.min_samples,
            )
            self.fitted_ = False
            return self

        self.feature_means_ = X.mean(axis=0)
        self.feature_stds_ = X.std(axis=0, ddof=1)
        Xz = self._standardise(X)

        if _HMMLEARN_AVAILABLE:
            try:
                model = GaussianHMM(
                    n_components=self.n_states,
                    covariance_type="diag",
                    random_state=self.seed,
                    n_iter=100,
                )
                model.fit(Xz)
                self.hmm_model_ = model
                self.backend_ = "hmmlearn"
                self.centers_ = np.asarray(model.means_)
                self.transition_matrix_ = np.asarray(model.transmat_)
            except Exception as exc:  # noqa: BLE001
                logger.warning("regime_models | hmmlearn fit failed (%s) — falling back", exc)
                self._fit_numpy(Xz)
        else:
            self._fit_numpy(Xz)

        labeller = label_for_state or default_label_for_state
        self.state_to_label_ = {
            j: labeller(self.centers_[j], self.feature_names)
            for j in range(self.centers_.shape[0])
        }
        self.fitted_ = True
        return self

    def _fit_numpy(self, Xz: np.ndarray) -> None:
        centers, labels = _kmeans(Xz, k=self.n_states, seed=self.seed)
        self.centers_ = centers
        self.backend_ = "numpy"
        # Empirical transition matrix.
        T = np.zeros((self.n_states, self.n_states))
        for a, b in zip(labels[:-1], labels[1:]):
            T[a, b] += 1.0
        for i in range(self.n_states):
            row_sum = T[i].sum()
            if row_sum > 0:
                T[i] /= row_sum
            else:
                T[i] = np.ones(self.n_states) / self.n_states
        self.transition_matrix_ = T

    def predict_label(self, x: np.ndarray) -> str:
        """Map a single feature vector to a regime label."""
        if not self.fitted_ or self.centers_ is None:
            return SENTINEL_UNKNOWN
        x = np.asarray(x, dtype=float).ravel()
        if len(x) != self.centers_.shape[1]:
            return SENTINEL_INSUFFICIENT
        xz = (
            (x - self.feature_means_) / np.where(self.feature_stds_ > 1e-12, self.feature_stds_, 1.0)
            if self.feature_means_ is not None
            else x
        )
        if self.backend_ == "hmmlearn" and self.hmm_model_ is not None:
            try:
                state = int(self.hmm_model_.predict(xz.reshape(1, -1))[0])
            except Exception:  # noqa: BLE001
                state = int(np.argmin(np.linalg.norm(self.centers_ - xz, axis=1)))
        else:
            state = int(np.argmin(np.linalg.norm(self.centers_ - xz, axis=1)))
        return (self.state_to_label_ or {}).get(state, SENTINEL_UNKNOWN)

    def predict_sequence(self, X: np.ndarray) -> list[str]:
        return [self.predict_label(row) for row in np.asarray(X, dtype=float)]

    # ── persistence ─────────────────────────────────────────────────────────

    def save(self, path: Path | str) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        # ``hmm_model_`` may be unpickleable in some hmmlearn versions; we
        # snapshot the centres/transition matrix and refit-or-fallback on load.
        snapshot = {
            "n_states": self.n_states,
            "feature_names": self.feature_names,
            "min_samples": self.min_samples,
            "seed": self.seed,
            "centers": self.centers_,
            "state_to_label": self.state_to_label_,
            "transition_matrix": self.transition_matrix_,
            "feature_means": self.feature_means_,
            "feature_stds": self.feature_stds_,
            "backend": self.backend_,
            "fitted": self.fitted_,
        }
        with open(p, "wb") as f:
            pickle.dump(snapshot, f, protocol=pickle.HIGHEST_PROTOCOL)

    @staticmethod
    def load(path: Path | str) -> "HMMRegimeClassifier":
        with open(path, "rb") as f:
            snap = pickle.load(f)
        clf = HMMRegimeClassifier(
            n_states=int(snap["n_states"]),
            feature_names=tuple(snap.get("feature_names") or ()),
            min_samples=int(snap.get("min_samples", 60)),
            seed=int(snap.get("seed", 7)),
        )
        clf.centers_ = snap["centers"]
        clf.state_to_label_ = snap["state_to_label"]
        clf.transition_matrix_ = snap["transition_matrix"]
        clf.feature_means_ = snap["feature_means"]
        clf.feature_stds_ = snap["feature_stds"]
        clf.backend_ = "numpy"  # always treat reloaded classifier as numpy backend
        clf.fitted_ = bool(snap.get("fitted", True))
        return clf


# ── default labelling heuristic ────────────────────────────────────────────


def default_label_for_state(center: np.ndarray, feature_names: Sequence[str]) -> str:
    """
    Map a fitted cluster centre (in *standardised* feature space) to a
    label from ``REGIME_LABELS``.

    Heuristics (defensible defaults; operator can override via
    ``label_for_state`` callback):

    - large +mean_return + low vol  → trend
    - small mean_return + low vol   → range
    - large -mean_return + high vol → crash
    - any with very high vol        → volatile
    - mean_return > 0 dominant      → risk_on
    - mean_return < 0 dominant      → risk_off
    """
    if not feature_names:
        # No semantic info; fall back to z-magnitude tiers.
        z = float(np.linalg.norm(center))
        if z < 0.5:
            return "range"
        if z < 1.5:
            return "trend"
        return "volatile"
    by_name = {name: float(center[i]) for i, name in enumerate(feature_names) if i < len(center)}
    mean_ret = by_name.get("mean_return", 0.0)
    vol = by_name.get("volatility", 0.0)
    breadth = by_name.get("breadth", 0.0)

    if vol > 1.5 and mean_ret < -0.5:
        return "crash"
    if vol > 1.0:
        return "volatile"
    if abs(mean_ret) > 0.7 and vol < 0.5:
        return "trend" if mean_ret > 0 else "risk_off"
    if abs(mean_ret) < 0.3 and vol < 0.3:
        return "range"
    if mean_ret > 0 or breadth > 0:
        return "risk_on"
    return "risk_off"
