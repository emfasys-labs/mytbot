"""
portfolio/covariance.py
=========================
Wave 4 — robust covariance estimators.

Two families:

- ``sample_covariance`` — vanilla sample covariance, fast but noisy
  on small N or wide P.
- ``ledoit_wolf_shrinkage`` — convex combination of the sample
  covariance and a constant-correlation target. Returns the shrunk
  matrix plus the shrinkage intensity ``delta`` for inspection.

Both helpers operate on a returns matrix ``R`` of shape ``(n_obs,
n_assets)`` and gracefully handle missing rows (NaN) by either dropping
or pairwise-deleting (configurable).

The Ledoit-Wolf implementation follows Ledoit & Wolf (2004) "Honey, I
shrunk the sample covariance matrix" — constant-correlation target
variant. We deliberately avoid sklearn so this module imports without
extra deps.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np


@dataclass
class CovarianceEstimate:
    matrix: np.ndarray
    method: str
    n_obs: int
    n_assets: int
    shrinkage: Optional[float] = None
    target: Optional[np.ndarray] = None
    condition_number: Optional[float] = None


def _clean_returns(R: np.ndarray, *, drop_na: bool) -> np.ndarray:
    arr = np.asarray(R, dtype=float)
    if arr.ndim == 1:
        arr = arr.reshape(-1, 1)
    if drop_na:
        arr = arr[~np.isnan(arr).any(axis=1)]
    return arr


def sample_covariance(
    R: np.ndarray,
    *,
    drop_na: bool = True,
) -> CovarianceEstimate:
    """Sample covariance with bias-corrected (n - 1) normaliser."""
    arr = _clean_returns(R, drop_na=drop_na)
    n_obs, n_assets = arr.shape
    if n_obs < 2:
        sigma = np.zeros((n_assets, n_assets))
    else:
        # rowvar=False: rows are observations, columns are variables.
        sigma = np.cov(arr, rowvar=False, ddof=1)
        if sigma.ndim == 0:  # single asset
            sigma = sigma.reshape(1, 1)
    return CovarianceEstimate(
        matrix=sigma,
        method="sample",
        n_obs=n_obs,
        n_assets=n_assets,
        condition_number=_safe_condition_number(sigma),
    )


def constant_correlation_target(sigma: np.ndarray) -> np.ndarray:
    """Build the Ledoit-Wolf constant-correlation shrinkage target."""
    p = sigma.shape[0]
    if p <= 1:
        return sigma.copy()
    var = np.diag(sigma)
    sd = np.sqrt(np.maximum(var, 1e-30))
    # Convert sigma -> correlation, average off-diagonals, rebuild.
    denom = np.outer(sd, sd)
    rho = np.where(denom > 0, sigma / denom, 0.0)
    np.fill_diagonal(rho, 1.0)
    off = rho[~np.eye(p, dtype=bool)]
    rho_bar = float(off.mean()) if off.size else 0.0
    target = rho_bar * denom
    np.fill_diagonal(target, var)
    return target


def ledoit_wolf_shrinkage(
    R: np.ndarray,
    *,
    drop_na: bool = True,
) -> CovarianceEstimate:
    """
    Shrink the sample covariance toward a constant-correlation target.

    The shrinkage intensity δ is computed in closed form so this is a
    single matrix expression — no optimisation. δ is clipped to [0, 1].
    """
    arr = _clean_returns(R, drop_na=drop_na)
    n, p = arr.shape
    if n < 2 or p < 2:
        # Fall back to the sample estimate.
        est = sample_covariance(arr, drop_na=False)
        est.method = "ledoit_wolf_fallback_sample"
        est.shrinkage = 0.0
        return est

    # Demean the returns.
    X = arr - arr.mean(axis=0, keepdims=True)
    sample = (X.T @ X) / (n - 1)
    target = constant_correlation_target(sample)

    # Estimate pi-hat: average squared deviation between r_{ij} and S_{ij}.
    X2 = X * X  # element-wise
    pi_mat = np.zeros((p, p))
    for i in range(p):
        for j in range(p):
            term = X[:, i] * X[:, j] - sample[i, j]
            pi_mat[i, j] = float(np.mean(term * term))
    pi_hat = float(pi_mat.sum())

    # gamma_hat = ||S - F||^2_F (Frobenius norm squared)
    diff = sample - target
    gamma_hat = float(np.sum(diff * diff))

    # rho_hat estimation: diagonal terms + off-diagonal cross-asymptotics.
    var = np.diag(sample)
    sd = np.sqrt(np.maximum(var, 1e-30))
    rho_diag = float(np.sum(np.diag(pi_mat)))
    rho_off = 0.0
    rho_bar = float(((sample / np.outer(sd, sd))[~np.eye(p, dtype=bool)]).mean()) if p > 1 else 0.0
    for i in range(p):
        for j in range(p):
            if i == j:
                continue
            term1 = (X2[:, i] * X[:, i] * X[:, j]) - sample[i, i] * sample[i, j]
            term2 = (X2[:, j] * X[:, i] * X[:, j]) - sample[j, j] * sample[i, j]
            theta_iij = float(np.mean(term1))
            theta_jji = float(np.mean(term2))
            denom_i = sd[i] if sd[i] > 0 else 1.0
            denom_j = sd[j] if sd[j] > 0 else 1.0
            rho_off += rho_bar * 0.5 * (
                (sd[j] / denom_i) * theta_iij + (sd[i] / denom_j) * theta_jji
            )
    rho_hat = rho_diag + rho_off

    if gamma_hat <= 0:
        delta = 0.0
    else:
        kappa = (pi_hat - rho_hat) / gamma_hat
        delta = float(min(1.0, max(0.0, kappa / max(1, n))))

    shrunk = delta * target + (1.0 - delta) * sample
    return CovarianceEstimate(
        matrix=shrunk,
        method="ledoit_wolf_constant_corr",
        n_obs=n,
        n_assets=p,
        shrinkage=delta,
        target=target,
        condition_number=_safe_condition_number(shrunk),
    )


def correlation_from_covariance(sigma: np.ndarray) -> np.ndarray:
    p = sigma.shape[0]
    if p == 0:
        return sigma.copy()
    sd = np.sqrt(np.maximum(np.diag(sigma), 1e-30))
    denom = np.outer(sd, sd)
    rho = np.where(denom > 0, sigma / denom, 0.0)
    np.fill_diagonal(rho, 1.0)
    return rho


def _safe_condition_number(m: np.ndarray) -> Optional[float]:
    if m.size == 0:
        return None
    try:
        cond = float(np.linalg.cond(m))
    except np.linalg.LinAlgError:
        return None
    return cond if np.isfinite(cond) else None
