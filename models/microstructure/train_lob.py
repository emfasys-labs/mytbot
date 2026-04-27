"""
models/microstructure/train_lob.py
====================================
Wave 10 — train a logistic baseline that maps LOB features to the sign
of the next short-horizon return.

NumPy-only fallback (no sklearn required) — same pattern as Waves 2/6.
"""

from __future__ import annotations

import math
import pickle
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import numpy as np
import pandas as pd

from backtest.validation import purged_time_series_splits
from models.calibration import IdentityCalibrator, make_calibrator
from models.feature_contracts import compute_feature_hash
from models.microstructure.features import FEATURE_NAMES, LOBFeatureSet
from models.schemas import FeatureSpec

try:
    from sklearn.linear_model import LogisticRegression  # type: ignore

    _SKLEARN_AVAILABLE = True
except Exception:  # noqa: BLE001
    LogisticRegression = None  # type: ignore
    _SKLEARN_AVAILABLE = False


# ── NumPy logistic fallback ────────────────────────────────────────────────


@dataclass
class _NPLogReg:
    coef_: Optional[np.ndarray] = None
    intercept_: float = 0.0
    feature_means_: Optional[np.ndarray] = None
    feature_stds_: Optional[np.ndarray] = None

    def fit(self, X: np.ndarray, y: np.ndarray, *, n_iter: int = 400, lr: float = 0.05) -> "_NPLogReg":
        X = np.asarray(X, dtype=float)
        y = np.asarray(y, dtype=float).ravel()
        if X.ndim == 1:
            X = X.reshape(-1, 1)
        n, k = X.shape
        mu = X.mean(axis=0)
        sd = X.std(axis=0)
        sd = np.where(sd > 1e-12, sd, 1.0)
        Xs = (X - mu) / sd
        w = np.zeros(k)
        b = 0.0
        for _ in range(int(n_iter)):
            z = Xs @ w + b
            p = 1.0 / (1.0 + np.exp(-np.clip(z, -30.0, 30.0)))
            err = p - y
            w -= lr * (Xs.T @ err / max(1, n))
            b -= lr * float(err.mean())
        self.coef_ = w
        self.intercept_ = b
        self.feature_means_ = mu
        self.feature_stds_ = sd
        return self

    def predict_proba(self, X: np.ndarray) -> np.ndarray:
        if self.coef_ is None:
            raise RuntimeError("fit before predict")
        X = np.asarray(X, dtype=float)
        if X.ndim == 1:
            X = X.reshape(1, -1)
        Xs = (X - self.feature_means_) / self.feature_stds_
        z = Xs @ self.coef_ + self.intercept_
        p = 1.0 / (1.0 + np.exp(-np.clip(z, -30.0, 30.0)))
        return np.column_stack([1.0 - p, p])


def _make_estimator():
    if _SKLEARN_AVAILABLE:
        return LogisticRegression(max_iter=400, solver="lbfgs")
    return _NPLogReg()


# ── persisted artefact ──────────────────────────────────────────────────────


@dataclass
class TrainedLOBForecaster:
    feature_specs: list[FeatureSpec]
    feature_contract_hash: str
    calibration_method: str = "platt"
    model: Any = None
    calibrator: Any = field(default_factory=IdentityCalibrator)
    metadata: dict[str, Any] = field(default_factory=dict)

    def predict(self, X: np.ndarray | pd.DataFrame) -> np.ndarray:
        if isinstance(X, pd.DataFrame):
            cols = [s.name for s in self.feature_specs]
            X = X.reindex(columns=cols).to_numpy(dtype=float)
        X = np.asarray(X, dtype=float)
        if X.ndim == 1:
            X = X.reshape(1, -1)
        if hasattr(self.model, "predict_proba"):
            raw = self.model.predict_proba(X)
            scores = raw[:, 1] if raw.ndim == 2 and raw.shape[1] >= 2 else raw.ravel()
        else:
            scores = 1.0 / (1.0 + np.exp(-np.asarray(self.model.decision_function(X))))
        return self.calibrator.transform(scores)

    def save(self, path: Path | str) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "wb") as f:
            pickle.dump(self, f, protocol=pickle.HIGHEST_PROTOCOL)

    @staticmethod
    def load(path: Path | str) -> "TrainedLOBForecaster":
        with open(path, "rb") as f:
            obj = pickle.load(f)
        if not isinstance(obj, TrainedLOBForecaster):
            raise TypeError(f"file does not contain TrainedLOBForecaster: {type(obj).__name__}")
        expected = compute_feature_hash(obj.feature_specs)
        if expected != obj.feature_contract_hash:
            raise ValueError("TrainedLOBForecaster feature_contract_hash mismatch — refusing to load")
        return obj


# ── eval report ─────────────────────────────────────────────────────────────


@dataclass
class LOBEvalReport:
    n_train: int
    n_oos: int
    metrics: dict[str, float] = field(default_factory=dict)
    feature_contract_hash: str = ""

    def summary(self) -> str:
        return f"lob | train={self.n_train} oos={self.n_oos} metrics={self.metrics}"


# ── training routine ────────────────────────────────────────────────────────


def _brier(y, p) -> float:
    return float(np.mean((np.asarray(p) - np.asarray(y)) ** 2))


def _logloss(y, p) -> float:
    eps = 1e-7
    p = np.clip(np.asarray(p), eps, 1.0 - eps)
    y = np.asarray(y)
    return float(-np.mean(y * np.log(p) + (1.0 - y) * np.log(1.0 - p)))


def train_imbalance_forecaster(
    *,
    dataset: LOBFeatureSet,
    calibration: str = "platt",
    n_splits: int = 4,
    embargo_bars: int = 5,
    metadata: Optional[dict[str, Any]] = None,
) -> tuple[TrainedLOBForecaster, LOBEvalReport]:
    if dataset.X.empty or dataset.y.empty:
        raise ValueError("empty dataset")
    cols = list(FEATURE_NAMES)
    missing = [c for c in cols if c not in dataset.X.columns]
    if missing:
        raise ValueError(f"missing feature columns: {missing}")
    X = dataset.X[cols].copy()
    y = dataset.y.astype(int).to_numpy()
    if np.unique(y).size < 2:
        raise ValueError("y must contain both classes")

    feature_specs = [FeatureSpec(c, "float64") for c in cols]
    contract_hash = compute_feature_hash(feature_specs)

    Xa = X.replace([np.inf, -np.inf], np.nan).fillna(0.0).to_numpy(dtype=float)

    splits = purged_time_series_splits(
        n_samples=len(y), n_splits=n_splits, embargo_bars=embargo_bars
    )

    cv_metrics: list[dict[str, float]] = []
    for (tr_lo, tr_hi), (te_lo, te_hi) in splits or []:
        if tr_hi <= tr_lo or te_hi <= te_lo:
            continue
        X_tr, y_tr = Xa[tr_lo:tr_hi], y[tr_lo:tr_hi]
        X_te, y_te = Xa[te_lo:te_hi], y[te_lo:te_hi]
        if np.unique(y_tr).size < 2 or np.unique(y_te).size < 2:
            continue
        clf = _make_estimator()
        clf.fit(X_tr, y_tr)
        raw = (
            clf.predict_proba(X_te)[:, 1]
            if hasattr(clf, "predict_proba")
            else 1.0 / (1.0 + np.exp(-np.asarray(clf.decision_function(X_te))))
        )
        cal = make_calibrator(calibration)
        tr_raw = (
            clf.predict_proba(X_tr)[:, 1]
            if hasattr(clf, "predict_proba")
            else 1.0 / (1.0 + np.exp(-np.asarray(clf.decision_function(X_tr))))
        )
        cal.fit(tr_raw, y_tr)
        p = np.asarray(cal.transform(raw))
        cv_metrics.append({
            "n": float(len(y_te)),
            "brier": _brier(y_te, p),
            "logloss": _logloss(y_te, p),
            "base_rate": float(y_te.mean()),
        })

    clf = _make_estimator()
    clf.fit(Xa, y)
    raw_full = (
        clf.predict_proba(Xa)[:, 1]
        if hasattr(clf, "predict_proba")
        else 1.0 / (1.0 + np.exp(-np.asarray(clf.decision_function(Xa))))
    )
    cal = make_calibrator(calibration)
    cal.fit(raw_full, y)

    artefact = TrainedLOBForecaster(
        feature_specs=feature_specs,
        feature_contract_hash=contract_hash,
        calibration_method=calibration,
        model=clf,
        calibrator=cal,
        metadata=dict(metadata or {}),
    )

    if cv_metrics:
        agg = {
            "brier_mean": float(np.mean([m["brier"] for m in cv_metrics])),
            "logloss_mean": float(np.mean([m["logloss"] for m in cv_metrics])),
            "base_rate_mean": float(np.mean([m["base_rate"] for m in cv_metrics])),
            "n_folds": float(len(cv_metrics)),
        }
    else:
        agg = {"n_folds": 0.0, "base_rate_mean": float(y.mean())}
    n_oos = int(sum(int(m["n"]) for m in cv_metrics))

    report = LOBEvalReport(
        n_train=int(len(y)),
        n_oos=n_oos,
        metrics=agg,
        feature_contract_hash=contract_hash,
    )
    return artefact, report
