"""
models/meta_label/train.py
===========================
Wave 2 — training entry point for the meta-labeller.

Design:

- The package import-load is light. sklearn / xgboost / catboost are
  optional imports — if missing we fall back to a NumPy logistic
  regression so unit tests run without ML deps.
- Calibration uses ``models.calibration`` (already NumPy-fallback
  capable from Wave 1).
- Validation uses purged k-fold splits from ``backtest.validation``.
- The fitted artefact ``TrainedMetaLabel`` is pickleable and pure-Python
  data + small numeric arrays, so we use ``pickle`` from stdlib for
  persistence (joblib not required).
- This module never registers with ``ModelRegistry`` — that's the
  operator's deliberate action via ``config/model_registry.yaml`` after
  inspecting the ``MetaLabelEvalReport``.
"""

from __future__ import annotations

import math
import pickle
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import numpy as np
import pandas as pd

from backtest.validation import deflated_sharpe_ratio  # noqa: F401  (re-exported helper)
from backtest.validation import purged_time_series_splits
from models.calibration import IdentityCalibrator, make_calibrator
from models.feature_contracts import compute_feature_hash
from models.schemas import FeatureSpec

try:  # Optional sklearn (preferred for production).
    from sklearn.ensemble import (  # type: ignore
        GradientBoostingClassifier,
        RandomForestClassifier,
    )
    from sklearn.linear_model import LogisticRegression  # type: ignore

    _SKLEARN_AVAILABLE = True
except Exception:  # noqa: BLE001
    GradientBoostingClassifier = None  # type: ignore
    RandomForestClassifier = None  # type: ignore
    LogisticRegression = None  # type: ignore
    _SKLEARN_AVAILABLE = False

try:  # Optional xgboost.
    from xgboost import XGBClassifier  # type: ignore

    _XGB_AVAILABLE = True
except Exception:  # noqa: BLE001
    XGBClassifier = None  # type: ignore
    _XGB_AVAILABLE = False


# ── lightweight NumPy logistic regression (fallback) ────────────────────────


@dataclass
class _NPLogReg:
    """
    Pure-NumPy logistic regression. Used when sklearn is unavailable.
    Persists as plain ndarrays under pickle.
    """

    coef_: Optional[np.ndarray] = None
    intercept_: float = 0.0
    feature_means_: Optional[np.ndarray] = None
    feature_stds_: Optional[np.ndarray] = None
    classes_: tuple[int, int] = (0, 1)

    def fit(self, X: np.ndarray, y: np.ndarray, *, n_iter: int = 400, lr: float = 0.05) -> "_NPLogReg":
        X = np.asarray(X, dtype=float)
        y = np.asarray(y, dtype=float).ravel()
        if X.ndim == 1:
            X = X.reshape(-1, 1)
        n, k = X.shape
        # Standardise features for numerical stability — same trick
        # sklearn's solver does internally.
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
            grad_w = Xs.T @ err / max(1, n)
            grad_b = float(err.mean())
            w -= lr * grad_w
            b -= lr * grad_b
        self.coef_ = w
        self.intercept_ = b
        self.feature_means_ = mu
        self.feature_stds_ = sd
        return self

    def predict_proba(self, X: np.ndarray) -> np.ndarray:
        if self.coef_ is None or self.feature_means_ is None or self.feature_stds_ is None:
            raise RuntimeError("_NPLogReg must be fit before predict")
        X = np.asarray(X, dtype=float)
        if X.ndim == 1:
            X = X.reshape(1, -1)
        Xs = (X - self.feature_means_) / self.feature_stds_
        z = Xs @ self.coef_ + self.intercept_
        p = 1.0 / (1.0 + np.exp(-np.clip(z, -30.0, 30.0)))
        # sklearn-compatible: column 0 = P(class 0), column 1 = P(class 1)
        return np.column_stack([1.0 - p, p])


# ── classifier factory ──────────────────────────────────────────────────────


def _make_classifier(kind: str) -> Any:
    k = (kind or "").strip().lower()
    if k in ("", "logreg", "logistic", "logistic_regression"):
        if _SKLEARN_AVAILABLE:
            return LogisticRegression(max_iter=400, solver="lbfgs", n_jobs=None)
        return _NPLogReg()
    if k in ("rf", "random_forest"):
        if not _SKLEARN_AVAILABLE:
            raise RuntimeError("Random Forest requested but sklearn is not installed")
        return RandomForestClassifier(
            n_estimators=200,
            max_depth=5,
            min_samples_leaf=10,
            random_state=42,
            class_weight="balanced_subsample",
        )
    if k in ("gbm", "gradient_boosting"):
        if not _SKLEARN_AVAILABLE:
            raise RuntimeError("Gradient Boosting requested but sklearn is not installed")
        return GradientBoostingClassifier(random_state=42)
    if k in ("xgb", "xgboost"):
        if not _XGB_AVAILABLE:
            raise RuntimeError("XGBoost requested but xgboost is not installed")
        return XGBClassifier(
            n_estimators=200,
            max_depth=5,
            learning_rate=0.05,
            objective="binary:logistic",
            eval_metric="logloss",
            random_state=42,
        )
    raise ValueError(f"unknown classifier kind: {kind!r}")


# ── persisted artefact ──────────────────────────────────────────────────────


@dataclass
class TrainedMetaLabel:
    """
    Pickleable trained-model bundle.

    ``model`` is the underlying classifier (sklearn estimator, ``_NPLogReg``,
    or xgboost). ``calibrator`` is one of the calibrators from
    ``models.calibration``. ``feature_specs`` defines the contract; the
    hash is convenience and MUST equal ``compute_feature_hash(feature_specs)``.
    """

    feature_specs: list[FeatureSpec]
    feature_contract_hash: str
    classifier_kind: str
    calibration_method: str
    model: Any = None
    calibrator: Any = field(default_factory=IdentityCalibrator)
    metadata: dict[str, Any] = field(default_factory=dict)

    def predict_proba(self, X: np.ndarray | pd.DataFrame) -> np.ndarray:
        if isinstance(X, pd.DataFrame):
            cols = [s.name for s in self.feature_specs]
            X = X.reindex(columns=cols).to_numpy(dtype=float)
        X = np.asarray(X, dtype=float)
        if X.ndim == 1:
            X = X.reshape(1, -1)
        if hasattr(self.model, "predict_proba"):
            raw = self.model.predict_proba(X)
            # column 1 is P(class=1).
            scores = raw[:, 1] if raw.ndim == 2 and raw.shape[1] >= 2 else raw.ravel()
        elif hasattr(self.model, "decision_function"):
            scores = 1.0 / (1.0 + np.exp(-np.asarray(self.model.decision_function(X))))
        else:
            raise RuntimeError("underlying model has no predict_proba / decision_function")
        return self.calibrator.transform(scores)

    def save(self, path: Path | str) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "wb") as f:
            pickle.dump(self, f, protocol=pickle.HIGHEST_PROTOCOL)

    @staticmethod
    def load(path: Path | str) -> "TrainedMetaLabel":
        with open(path, "rb") as f:
            obj = pickle.load(f)
        if not isinstance(obj, TrainedMetaLabel):
            raise TypeError(
                f"file does not contain a TrainedMetaLabel: got {type(obj).__name__}"
            )
        # Verify the hash matches the feature list — mutating the
        # specs after fit would invalidate the contract.
        expected = compute_feature_hash(obj.feature_specs)
        if expected != obj.feature_contract_hash:
            raise ValueError(
                "TrainedMetaLabel feature_contract_hash mismatch — refusing to load"
            )
        return obj


# ── evaluation report ───────────────────────────────────────────────────────


@dataclass
class MetaLabelEvalReport:
    n_train: int
    n_oos: int
    classifier_kind: str
    calibration_method: str
    metrics: dict[str, float] = field(default_factory=dict)
    cv_metrics: list[dict[str, float]] = field(default_factory=list)
    feature_contract_hash: str = ""

    def summary(self) -> str:
        lines = [
            f"meta_label | clf={self.classifier_kind} calib={self.calibration_method}",
            f"  rows train={self.n_train} oos={self.n_oos}",
            f"  metrics={self.metrics}",
            f"  contract_hash={self.feature_contract_hash}",
        ]
        return "\n".join(lines)


# ── main training routine ───────────────────────────────────────────────────


def _binary_brier(y_true: np.ndarray, p: np.ndarray) -> float:
    return float(np.mean((p - y_true) ** 2))


def _binary_logloss(y_true: np.ndarray, p: np.ndarray) -> float:
    eps = 1e-7
    p = np.clip(p, eps, 1.0 - eps)
    return float(-np.mean(y_true * np.log(p) + (1.0 - y_true) * np.log(1.0 - p)))


def _hit_rate(y_true: np.ndarray, p: np.ndarray, threshold: float) -> float:
    take = p >= threshold
    if take.sum() == 0:
        return 0.0
    return float(np.mean(y_true[take]))


def train_meta_label_model(
    *,
    X: pd.DataFrame,
    y: pd.Series,
    feature_specs: list[FeatureSpec],
    classifier: str = "logreg",
    calibration: str = "isotonic",
    n_splits: int = 5,
    embargo_bars: int = 5,
    threshold_for_metrics: float = 0.55,
    metadata: Optional[dict[str, Any]] = None,
) -> tuple[TrainedMetaLabel, MetaLabelEvalReport]:
    """
    Train a meta-label classifier with purged-CV evaluation.

    Returns ``(trained_artefact, eval_report)``. The artefact is fit on
    the *full* dataset; the eval report contains out-of-sample metrics
    from the purged k-fold splits.

    Caller is responsible for:
      - constructing ``X``/``y`` via ``models.meta_label.dataset.build_dataset_from_close``
        (so leakage is enforced),
      - choosing whether to register the artefact (and at what status)
        via ``config/model_registry.yaml``.
    """
    if X.empty or y.empty:
        raise ValueError("empty dataset")
    if len(X) != len(y):
        raise ValueError("X/y row count mismatch")
    cols = [s.name for s in feature_specs]
    if list(X.columns) != cols:
        # Reorder X to match feature contract — also fails loudly on
        # missing columns.
        missing = [c for c in cols if c not in X.columns]
        if missing:
            raise ValueError(f"feature columns missing from X: {missing}")
        X = X[cols].copy()

    contract_hash = compute_feature_hash(feature_specs)

    Xa = X.replace([np.inf, -np.inf], np.nan).fillna(0.0).to_numpy(dtype=float)
    ya = y.astype(int).to_numpy()

    if np.unique(ya).size < 2:
        raise ValueError("y must contain both classes (0 and 1)")

    # --- purged CV ----------------------------------------------------------
    splits = purged_time_series_splits(
        n_samples=len(ya),
        n_splits=n_splits,
        embargo_bars=embargo_bars,
    )

    cv_metrics: list[dict[str, float]] = []
    if splits:
        for (tr_lo, tr_hi), (te_lo, te_hi) in splits:
            if tr_hi <= tr_lo or te_hi <= te_lo:
                continue
            X_tr, y_tr = Xa[tr_lo:tr_hi], ya[tr_lo:tr_hi]
            X_te, y_te = Xa[te_lo:te_hi], ya[te_lo:te_hi]
            if np.unique(y_tr).size < 2 or np.unique(y_te).size < 2:
                continue
            clf = _make_classifier(classifier)
            clf.fit(X_tr, y_tr)
            raw = (
                clf.predict_proba(X_te)[:, 1]
                if hasattr(clf, "predict_proba")
                else np.asarray(clf.decision_function(X_te))  # type: ignore[attr-defined]
            )
            cal = make_calibrator(calibration)
            # In-fold calibration would need a held-out slice; use train
            # raw predictions as the calibration set for the per-fold
            # metric — this is conservative (slightly optimistic on
            # train), and is purely for OOS reporting, not deployment.
            tr_raw = (
                clf.predict_proba(X_tr)[:, 1]
                if hasattr(clf, "predict_proba")
                else np.asarray(clf.decision_function(X_tr))  # type: ignore[attr-defined]
            )
            cal.fit(tr_raw, y_tr)
            p_te = np.asarray(cal.transform(raw))
            cv_metrics.append(
                {
                    "n": float(len(y_te)),
                    "brier": _binary_brier(y_te, p_te),
                    "logloss": _binary_logloss(y_te, p_te),
                    "hit_rate@thr": _hit_rate(y_te, p_te, threshold_for_metrics),
                    "base_rate": float(y_te.mean()),
                }
            )

    # --- final fit on full data --------------------------------------------
    clf = _make_classifier(classifier)
    clf.fit(Xa, ya)
    raw_full = (
        clf.predict_proba(Xa)[:, 1]
        if hasattr(clf, "predict_proba")
        else np.asarray(clf.decision_function(Xa))  # type: ignore[attr-defined]
    )
    calibrator = make_calibrator(calibration)
    calibrator.fit(raw_full, ya)

    artefact = TrainedMetaLabel(
        feature_specs=feature_specs,
        feature_contract_hash=contract_hash,
        classifier_kind=classifier,
        calibration_method=calibration,
        model=clf,
        calibrator=calibrator,
        metadata=dict(metadata or {}),
    )

    # --- aggregate metrics --------------------------------------------------
    if cv_metrics:
        agg = {
            "brier_mean": float(np.mean([m["brier"] for m in cv_metrics])),
            "logloss_mean": float(np.mean([m["logloss"] for m in cv_metrics])),
            "hit_rate@thr_mean": float(np.mean([m["hit_rate@thr"] for m in cv_metrics])),
            "base_rate_mean": float(np.mean([m["base_rate"] for m in cv_metrics])),
            "n_folds": float(len(cv_metrics)),
        }
    else:
        agg = {
            "brier_mean": math.nan,
            "logloss_mean": math.nan,
            "hit_rate@thr_mean": math.nan,
            "base_rate_mean": float(ya.mean()),
            "n_folds": 0.0,
        }

    n_oos = int(sum(int(m["n"]) for m in cv_metrics))

    report = MetaLabelEvalReport(
        n_train=int(len(ya)),
        n_oos=n_oos,
        classifier_kind=classifier,
        calibration_method=calibration,
        metrics=agg,
        cv_metrics=cv_metrics,
        feature_contract_hash=contract_hash,
    )
    return artefact, report
