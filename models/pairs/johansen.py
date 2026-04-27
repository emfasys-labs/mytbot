"""
models/pairs/johansen.py
==========================
Wave 5 — cointegration screens.

Two complementary tests:

- ``engle_granger_test`` — single-pair OLS hedge ratio + ADF-style
  stationarity statistic on the residuals. Cheap, leakage-safe, and
  gives the operator a per-pair p-value-like screen using canned
  critical values.
- ``johansen_eigen_test`` — VECM eigendecomposition over a small
  multivariate system. Reports the eigenvalues so the operator can
  inspect cointegration *rank* visually; full trace-statistic critical
  values require statsmodels and are out of scope for this NumPy-only
  implementation.

Both routines are deterministic and side-effect-free.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd


# ── Engle-Granger ───────────────────────────────────────────────────────────


# ADF critical values for a regression with constant (no trend), large-N
# asymptotic. From Davidson-MacKinnon (1993). Used as a screen, not a
# substitute for a real statsmodels.adfuller call.
_ADF_CRIT = {0.01: -3.43, 0.05: -2.86, 0.10: -2.57}


@dataclass
class EngleGrangerResult:
    beta: float
    intercept: float
    adf_stat: float
    p_value_estimate: float  # interpolated from the canned critical values
    is_cointegrated_5pct: bool
    n_obs: int
    notes: str = ""


def _ols_simple(y: np.ndarray, x: np.ndarray) -> tuple[float, float]:
    """OLS y = a + b * x. Returns (a, b)."""
    X = np.column_stack([np.ones(len(x)), x])
    coef, *_ = np.linalg.lstsq(X, y, rcond=None)
    return float(coef[0]), float(coef[1])


def _adf_t_stat(series: np.ndarray, *, lags: int = 1) -> Optional[float]:
    """
    ADF t-statistic on β in:
        Δy_t = α + β * y_{t-1} + Σ_{i=1..lags} γ_i * Δy_{t-i} + ε_t

    Returns ``None`` when the regression matrix is singular. The
    statistic is *unsigned* — Davidson-MacKinnon negative criticals
    apply.
    """
    y = np.asarray(series, dtype=float).ravel()
    n_total = len(y)
    if n_total < max(20, lags + 10):
        return None

    dy = np.diff(y)
    y_lag = y[:-1]

    # Build lag matrix of Δy.
    rows = []
    for k in range(1, lags + 1):
        rows.append(np.r_[np.full(k, np.nan), dy[:-k]])
    lag_block = np.column_stack(rows) if rows else np.zeros((len(dy), 0))

    keep = ~np.isnan(lag_block).any(axis=1) if lag_block.size else np.ones(len(dy), dtype=bool)
    if int(keep.sum()) < 15:
        return None
    dy_k = dy[keep]
    y_lag_k = y_lag[keep]
    lag_block_k = lag_block[keep] if lag_block.size else np.zeros((len(dy_k), 0))

    X = np.column_stack([np.ones(len(dy_k)), y_lag_k, lag_block_k])
    try:
        coef, _, _, _ = np.linalg.lstsq(X, dy_k, rcond=None)
    except np.linalg.LinAlgError:
        return None
    resid = dy_k - X @ coef
    df = len(dy_k) - X.shape[1]
    if df <= 0:
        return None
    sigma2 = float(resid @ resid) / df
    try:
        cov = sigma2 * np.linalg.inv(X.T @ X)
    except np.linalg.LinAlgError:
        return None
    se_beta = math.sqrt(max(0.0, float(cov[1, 1])))
    if se_beta <= 0:
        return None
    return float(coef[1] / se_beta)


def _interpolate_adf_p(stat: float) -> float:
    """Coarse mapping from ADF stat to a one-sided p-value approximation."""
    # More negative stat ⇒ stronger evidence against unit root.
    if stat <= _ADF_CRIT[0.01]:
        return 0.005
    if stat <= _ADF_CRIT[0.05]:
        # Linear interp between 1% and 5%.
        a, b = _ADF_CRIT[0.01], _ADF_CRIT[0.05]
        frac = (stat - a) / (b - a) if b > a else 0.0
        return float(0.01 + frac * (0.05 - 0.01))
    if stat <= _ADF_CRIT[0.10]:
        a, b = _ADF_CRIT[0.05], _ADF_CRIT[0.10]
        frac = (stat - a) / (b - a) if b > a else 0.0
        return float(0.05 + frac * (0.10 - 0.05))
    # Stat too high — generally not cointegrated. Cap at a coarse 0.5.
    return 0.5


def engle_granger_test(
    y: pd.Series,
    x: pd.Series,
    *,
    adf_lags: int = 1,
) -> EngleGrangerResult:
    """
    Two-step Engle-Granger:
        1. OLS regression of ``y`` on ``x`` (with intercept).
        2. ADF-style stationarity test on residuals.

    The result's ``is_cointegrated_5pct`` flag is the operator-friendly
    one-line answer; ``p_value_estimate`` is interpolated from the
    canned MacKinnon critical values and is a *screen*, not a rigorous
    p-value.
    """
    a, b = pd.concat([y.astype(float), x.astype(float)], axis=1, join="inner").dropna().to_numpy().T
    n = len(a)
    if n < 30:
        return EngleGrangerResult(
            beta=float("nan"),
            intercept=float("nan"),
            adf_stat=float("nan"),
            p_value_estimate=1.0,
            is_cointegrated_5pct=False,
            n_obs=n,
            notes="insufficient_data",
        )
    intercept, beta = _ols_simple(a, b)
    resid = a - intercept - beta * b
    stat = _adf_t_stat(resid, lags=max(0, int(adf_lags)))
    if stat is None or not math.isfinite(stat):
        return EngleGrangerResult(
            beta=beta, intercept=intercept,
            adf_stat=float("nan"), p_value_estimate=1.0,
            is_cointegrated_5pct=False, n_obs=n,
            notes="adf_failed",
        )
    p = _interpolate_adf_p(stat)
    return EngleGrangerResult(
        beta=beta,
        intercept=intercept,
        adf_stat=float(stat),
        p_value_estimate=float(p),
        is_cointegrated_5pct=bool(stat <= _ADF_CRIT[0.05]),
        n_obs=n,
    )


# ── Johansen-lite (VECM eigendecomposition) ─────────────────────────────────


@dataclass
class JohansenResult:
    eigenvalues: list[float]
    eigenvectors: list[list[float]]  # columns are vectors
    largest_eigenvector: list[float]
    n_obs: int
    notes: str = ""


def johansen_eigen_test(prices: pd.DataFrame) -> JohansenResult:
    """
    Run the eigendecomposition step of Johansen's procedure.

    The procedure (simplified):
        1. ΔY_t = Π Y_{t-1} + B X_t + ε_t  — VECM(0).
        2. Run an auxiliary regression of ΔY_t and Y_{t-1} on the
           intercept-only X_t, take residuals R_0 and R_1.
        3. Solve the eigenvalue problem |λ S_11 - S_10' S_00^{-1} S_10| = 0
           where S_ij = R_i' R_j / T.
        4. Eigenvalues in [0, 1) — larger ⇒ stronger cointegration.

    For the trace test against critical values you need statsmodels
    (``statsmodels.tsa.vector_ar.vecm.coint_johansen``). This routine
    deliberately stops at the eigenvalues so the operator has a numeric
    screen without a heavy dependency.
    """
    Y = prices.dropna().to_numpy(dtype=float)
    n, k = Y.shape
    if n < max(50, 5 * k) or k < 2:
        return JohansenResult(
            eigenvalues=[],
            eigenvectors=[],
            largest_eigenvector=[],
            n_obs=n,
            notes="insufficient_data",
        )

    dY = np.diff(Y, axis=0)         # shape (n-1, k)
    Y_lag = Y[:-1]                  # shape (n-1, k)
    T = dY.shape[0]
    if T <= k + 1:
        return JohansenResult([], [], [], n, "too_few_rows")

    X = np.ones((T, 1))             # intercept only
    # OLS residuals R0 = M_X dY, R1 = M_X Y_lag.
    Px = X @ np.linalg.solve(X.T @ X, X.T)
    M = np.eye(T) - Px
    R0 = M @ dY
    R1 = M @ Y_lag

    S00 = (R0.T @ R0) / T
    S01 = (R0.T @ R1) / T
    S10 = (R1.T @ R0) / T
    S11 = (R1.T @ R1) / T

    try:
        S00_inv = np.linalg.inv(S00)
        # Solve generalised eigenvalue problem S10' S00^{-1} S10 v = λ S11 v.
        A = S10.T @ S00_inv @ S10
        # Symmetrise for numerical stability.
        A = 0.5 * (A + A.T)
        S11_sym = 0.5 * (S11 + S11.T)
        eigvals, eigvecs = _generalised_eig_sym(A, S11_sym)
    except np.linalg.LinAlgError:
        return JohansenResult([], [], [], n, "singular_covariance")

    # Sort descending.
    idx = np.argsort(-eigvals)
    eigvals = np.clip(eigvals[idx], 0.0, 1.0)
    eigvecs = eigvecs[:, idx]
    largest = eigvecs[:, 0].tolist()

    return JohansenResult(
        eigenvalues=[float(v) for v in eigvals],
        eigenvectors=[[float(v) for v in eigvecs[:, j]] for j in range(eigvecs.shape[1])],
        largest_eigenvector=[float(v) for v in largest],
        n_obs=n,
    )


def _generalised_eig_sym(A: np.ndarray, B: np.ndarray):
    """
    Solve A v = λ B v for symmetric A and SPD-ish B without scipy.

    Strategy: Cholesky-decompose B = L L^T (with a small ridge on
    failure), transform to standard eigenproblem on
    (L^{-1}) A (L^{-T}), then map vectors back via L^{-T}.
    """
    p = B.shape[0]
    eps = 1e-10
    for ridge in (0.0, eps, 1e-6, 1e-3):
        try:
            L = np.linalg.cholesky(B + ridge * np.eye(p))
            break
        except np.linalg.LinAlgError:
            continue
    else:
        raise np.linalg.LinAlgError("B not positive-definite even with ridge")

    Linv = np.linalg.inv(L)
    M = Linv @ A @ Linv.T
    M = 0.5 * (M + M.T)
    w, V = np.linalg.eigh(M)
    # Map vectors back: v = L^{-T} u.
    Linv_T = Linv.T
    Vmapped = Linv_T @ V
    return w, Vmapped
