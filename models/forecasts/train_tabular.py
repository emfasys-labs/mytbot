"""
models/forecasts/train_tabular.py
==================================
Wave 6 — training entry for tabular forecast models.

Mirrors ``models/meta_label/train.py`` design: NumPy-only fallbacks for
both regression and classification so unit tests run without sklearn.
``arch`` / ``xgboost`` / ``catboost`` remain optional.
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
from models.forecasts.dataset import ForecastDataset
from models.schemas import FeatureSpec

try:  # Optional sklearn.
    from sklearn.ensemble import (  # type: ignore
        GradientBoostingClassifier,
        GradientBoostingRegressor,
        RandomForestClassifier,
        RandomForestRegressor,
    )
    from sklearn.linear_model import LinearRegression, LogisticRegression, Ridge  # type: ignore

    _SKLEARN_AVAILABLE = True
except Exception:  # noqa: BLE001
    GradientBoostingClassifier = None  # type: ignore
    GradientBoostingRegressor = None  # type: ignore
    RandomForestClassifier = None  # type: ignore
    RandomForestRegressor = None  # type: ignore
    LinearRegression = None  # type: ignore
    LogisticRegression = None  # type: ignore
    Ridge = None  # type: ignore
    _SKLEARN_AVAILABLE = False

try:
    from xgboost import XGBClassifier, XGBRegressor  # type: ignore

    _XGB_AVAILABLE = True
except Exception:  # noqa: BLE001
    XGBClassifier = None  # type: ignore
    XGBRegressor = None  # type: ignore
    _XGB_AVAILABLE = False


# ── NumPy fallback estimators ───────────────────────────────────────────────


@dataclass
class _NPRidge:
    """Ridge regression solved in closed form."""

    alpha: float = 1.0
    coef_: Optional[np.ndarray] = None
    intercept_: float = 0.0
    feature_means_: Optional[np.ndarray] = None
    feature_stds_: Optional[np.ndarray] = None

    def fit(self, X: np.ndarray, y: np.ndarray) -> "_NPRidge":
        X = np.asarray(X, dtype=float)
        y = np.asarray(y, dtype=float).ravel()
        mu = X.mean(axis=0)
        sd = X.std(axis=0)
        sd = np.where(sd > 1e-12, sd, 1.0)
        Xs = (X - mu) / sd
        n, p = Xs.shape
        # Closed-form ridge: w = (X.T X + alpha I)^-1 X.T (y - y_mean)
        y_mean = float(y.mean())
        ytilde = y - y_mean
        gram = Xs.T @ Xs + self.alpha * np.eye(p)
        coef = np.linalg.solve(gram, Xs.T @ ytilde)
        self.coef_ = coef
        self.intercept_ = y_mean
        self.feature_means_ = mu
        self.feature_stds_ = sd
        return self

    def predict(self, X: np.ndarray) -> np.ndarray:
        if self.coef_ is None:
            raise RuntimeError("_NPRidge must be fit before predict")
        X = np.asarray(X, dtype=float)
        if X.ndim == 1:
            X = X.reshape(1, -1)
        Xs = (X - self.feature_means_) / self.feature_stds_
        return Xs @ self.coef_ + self.intercept_


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
            raise RuntimeError("_NPLogReg must be fit before predict")
        X = np.asarray(X, dtype=float)
        if X.ndim == 1:
            X = X.reshape(1, -1)
        Xs = (X - self.feature_means_) / self.feature_stds_
        z = Xs @ self.coef_ + self.intercept_
        p = 1.0 / (1.0 + np.exp(-np.clip(z, -30.0, 30.0)))
        return np.column_stack([1.0 - p, p])


def _make_classifier(kind: str) -> Any:
    k = (kind or "").strip().lower()
    if k in ("", "logreg", "logistic", "logistic_regression"):
        if _SKLEARN_AVAILABLE:
            return LogisticRegression(max_iter=400, solver="lbfgs")
        return _NPLogReg()
    if k in ("rf", "random_forest"):
        if not _SKLEARN_AVAILABLE:
            raise RuntimeError("Random Forest requires sklearn")
        return RandomForestClassifier(
            n_estimators=200, max_depth=5, min_samples_leaf=10,
            random_state=42, class_weight="balanced_subsample",
        )
    if k in ("gbm", "gradient_boosting"):
        if not _SKLEARN_AVAILABLE:
            raise RuntimeError("Gradient Boosting requires sklearn")
        return GradientBoostingClassifier(random_state=42)
    if k in ("xgb", "xgboost"):
        if not _XGB_AVAILABLE:
            raise RuntimeError("XGBoost not installed")
        return XGBClassifier(n_estimators=200, max_depth=5, learning_rate=0.05,
                             objective="binary:logistic", eval_metric="logloss", random_state=42)
    raise ValueError(f"unknown classifier kind: {kind!r}")


def _make_regressor(kind: str) -> Any:
    k = (kind or "").strip().lower()
    if k in ("", "ridge", "linear"):
        if _SKLEARN_AVAILABLE:
            return Ridge(alpha=1.0)
        return _NPRidge(alpha=1.0)
    if k in ("rf", "random_forest"):
        if not _SKLEARN_AVAILABLE:
            raise RuntimeError("Random Forest requires sklearn")
        return RandomForestRegressor(
            n_estimators=200, max_depth=5, min_samples_leaf=10, random_state=42,
        )
    if k in ("gbm", "gradient_boosting"):
        if not _SKLEARN_AVAILABLE:
            raise RuntimeError("Gradient Boosting requires sklearn")
        return GradientBoostingRegressor(random_state=42)
    if k in ("xgb", "xgboost"):
        if not _XGB_AVAILABLE:
            raise RuntimeError("XGBoost not installed")
        return XGBRegressor(n_estimators=200, max_depth=5, learning_rate=0.05, random_state=42)
    raise ValueError(f"unknown regressor kind: {kind!r}")


# ── persisted artefact ──────────────────────────────────────────────────────


@dataclass
class TrainedForecastModel:
    """Pickleable forecast artefact (regression or classification)."""

    feature_specs: list[FeatureSpec]
    feature_contract_hash: str
    target_kind: str
    horizon: int
    is_classification: bool
    estimator_kind: str
    calibration_method: str
    model: Any = None
    calibrator: Any = field(default_factory=IdentityCalibrator)
    metadata: dict[str, Any] = field(default_factory=dict)

    def predict(self, X: np.ndarray | pd.DataFrame) -> np.ndarray:
        """Regression: predicted y. Classification: calibrated probability."""
        if isinstance(X, pd.DataFrame):
            cols = [s.name for s in self.feature_specs]
            X = X.reindex(columns=cols).to_numpy(dtype=float)
        X = np.asarray(X, dtype=float)
        if X.ndim == 1:
            X = X.reshape(1, -1)
        if self.is_classification:
            if hasattr(self.model, "predict_proba"):
                raw = self.model.predict_proba(X)
                scores = raw[:, 1] if raw.ndim == 2 and raw.shape[1] >= 2 else raw.ravel()
            else:
                scores = 1.0 / (1.0 + np.exp(-np.asarray(self.model.decision_function(X))))
            return self.calibrator.transform(scores)
        # Regression — calibrator is identity unless explicitly set.
        return self.model.predict(X)

    def save(self, path: Path | str) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "wb") as f:
            pickle.dump(self, f, protocol=pickle.HIGHEST_PROTOCOL)

    @staticmethod
    def load(path: Path | str) -> "TrainedForecastModel":
        with open(path, "rb") as f:
            obj = pickle.load(f)
        if not isinstance(obj, TrainedForecastModel):
            raise TypeError(f"file does not contain TrainedForecastModel: {type(obj).__name__}")
        expected = compute_feature_hash(obj.feature_specs)
        if expected != obj.feature_contract_hash:
            raise ValueError("TrainedForecastModel feature_contract_hash mismatch — refusing to load")
        return obj


# ── eval report ─────────────────────────────────────────────────────────────


@dataclass
class ForecastEvalReport:
    n_train: int
    n_oos: int
    target_kind: str
    horizon: int
    estimator_kind: str
    calibration_method: str
    is_classification: bool
    metrics: dict[str, float] = field(default_factory=dict)
    cv_metrics: list[dict[str, float]] = field(default_factory=list)
    feature_contract_hash: str = ""

    def summary(self) -> str:
        return (
            f"forecast | target={self.target_kind} h={self.horizon} "
            f"est={self.estimator_kind} calib={self.calibration_method} "
            f"rows train={self.n_train} oos={self.n_oos} metrics={self.metrics}"
        )


# ── training routine ────────────────────────────────────────────────────────


def _ic(y: np.ndarray, yhat: np.ndarray) -> float:
    if len(y) < 3:
        return float("nan")
    a = pd.Series(y).rank(pct=True).to_numpy()
    b = pd.Series(yhat).rank(pct=True).to_numpy()
    if np.std(a) == 0 or np.std(b) == 0:
        return 0.0
    return float(np.corrcoef(a, b)[0, 1])


def _hit_rate(y: np.ndarray, yhat: np.ndarray) -> float:
    if len(y) == 0:
        return float("nan")
    sign_y = np.sign(y)
    sign_yh = np.sign(yhat)
    mask = (sign_y != 0) & (sign_yh != 0)
    if mask.sum() == 0:
        return float("nan")
    return float((sign_y[mask] == sign_yh[mask]).mean())


def _brier(y: np.ndarray, p: np.ndarray) -> float:
    return float(np.mean((p - y) ** 2))


def _logloss(y: np.ndarray, p: np.ndarray) -> float:
    eps = 1e-7
    p = np.clip(p, eps, 1 - eps)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))


def train_forecast_model(
    *,
    dataset: ForecastDataset,
    feature_specs: list[FeatureSpec],
    estimator: str = "ridge",
    calibration: str = "none",
    n_splits: int = 5,
    embargo_bars: int = 5,
    metadata: Optional[dict[str, Any]] = None,
) -> tuple[TrainedForecastModel, ForecastEvalReport]:
    """Fit + purged-CV evaluate. Caller decides whether to register."""
    X, y = dataset.X, dataset.y
    if X.empty or y.empty:
        raise ValueError("empty dataset")
    if len(X) != len(y):
        raise ValueError("X/y row count mismatch")
    cols = [s.name for s in feature_specs]
    missing = [c for c in cols if c not in X.columns]
    if missing:
        raise ValueError(f"feature columns missing from X: {missing}")
    X = X[cols].copy()
    contract_hash = compute_feature_hash(feature_specs)

    Xa = X.replace([np.inf, -np.inf], np.nan).fillna(0.0).to_numpy(dtype=float)
    ya = y.astype(float).to_numpy()
    is_clf = dataset.is_classification

    if is_clf:
        # Require both classes for training.
        if np.unique(ya).size < 2:
            raise ValueError("classification target lacks both classes")
        estimator = estimator if estimator else "logreg"
    else:
        estimator = estimator if estimator else "ridge"

    splits = purged_time_series_splits(
        n_samples=len(ya), n_splits=n_splits, embargo_bars=embargo_bars
    )

    cv_metrics: list[dict[str, float]] = []
    for (tr_lo, tr_hi), (te_lo, te_hi) in splits or []:
        if tr_hi <= tr_lo or te_hi <= te_lo:
            continue
        X_tr, y_tr = Xa[tr_lo:tr_hi], ya[tr_lo:tr_hi]
        X_te, y_te = Xa[te_lo:te_hi], ya[te_lo:te_hi]
        if is_clf and (np.unique(y_tr).size < 2 or np.unique(y_te).size < 2):
            continue
        if is_clf:
            clf = _make_classifier(estimator)
            clf.fit(X_tr, y_tr.astype(int))
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
        else:
            reg = _make_regressor(estimator)
            reg.fit(X_tr, y_tr)
            yhat = np.asarray(reg.predict(X_te), dtype=float).ravel()
            cv_metrics.append({
                "n": float(len(y_te)),
                "ic": _ic(y_te, yhat),
                "hit_rate": _hit_rate(y_te, yhat),
                "rmse": float(np.sqrt(np.mean((yhat - y_te) ** 2))),
            })

    # Final fit on full data.
    if is_clf:
        clf = _make_classifier(estimator)
        clf.fit(Xa, ya.astype(int))
        raw_full = (
            clf.predict_proba(Xa)[:, 1]
            if hasattr(clf, "predict_proba")
            else 1.0 / (1.0 + np.exp(-np.asarray(clf.decision_function(Xa))))
        )
        cal = make_calibrator(calibration)
        cal.fit(raw_full, ya)
        artefact = TrainedForecastModel(
            feature_specs=feature_specs,
            feature_contract_hash=contract_hash,
            target_kind=dataset.target_kind,
            horizon=dataset.horizon,
            is_classification=True,
            estimator_kind=estimator,
            calibration_method=calibration,
            model=clf,
            calibrator=cal,
            metadata=dict(metadata or {}),
        )
    else:
        reg = _make_regressor(estimator)
        reg.fit(Xa, ya)
        artefact = TrainedForecastModel(
            feature_specs=feature_specs,
            feature_contract_hash=contract_hash,
            target_kind=dataset.target_kind,
            horizon=dataset.horizon,
            is_classification=False,
            estimator_kind=estimator,
            calibration_method="none",
            model=reg,
            calibrator=IdentityCalibrator(),
            metadata=dict(metadata or {}),
        )

    if cv_metrics:
        if is_clf:
            agg = {
                "brier_mean": float(np.mean([m["brier"] for m in cv_metrics])),
                "logloss_mean": float(np.mean([m["logloss"] for m in cv_metrics])),
                "base_rate_mean": float(np.mean([m["base_rate"] for m in cv_metrics])),
                "n_folds": float(len(cv_metrics)),
            }
        else:
            agg = {
                "ic_mean": float(np.nanmean([m["ic"] for m in cv_metrics])),
                "hit_rate_mean": float(np.nanmean([m["hit_rate"] for m in cv_metrics])),
                "rmse_mean": float(np.mean([m["rmse"] for m in cv_metrics])),
                "n_folds": float(len(cv_metrics)),
            }
    else:
        agg = {"n_folds": 0.0}
    n_oos = int(sum(int(m["n"]) for m in cv_metrics))

    report = ForecastEvalReport(
        n_train=int(len(ya)),
        n_oos=n_oos,
        target_kind=dataset.target_kind,
        horizon=dataset.horizon,
        estimator_kind=estimator,
        calibration_method=calibration if is_clf else "none",
        is_classification=is_clf,
        metrics=agg,
        cv_metrics=cv_metrics,
        feature_contract_hash=contract_hash,
    )
    return artefact, report
