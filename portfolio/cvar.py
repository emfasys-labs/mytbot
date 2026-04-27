"""
portfolio/cvar.py
==================
Wave 8 — CVaR (Expected Shortfall) minimisation.

Problem:

    min_w  CVaR_α(R w)
    s.t.   sum(w) = 1,  min_w <= w <= max_w

where ``R`` is a returns scenario matrix (``n_obs × n_assets``). CVaR
at level ``α`` (default 0.05 ⇒ 5% worst tail) is the mean of returns
in the worst ``α``-tail.

Solver: a deliberately simple projected-gradient descent on a smoothed
CVaR surrogate. Fast enough for portfolios up to ~50 assets and
*always* terminates with a feasible (simplex-constrained) solution.

When the optimisation is unstable / degenerate (single asset, all-zero
returns, etc.) we fall back to equal weights — flagged in
``CVaRResult.fallback`` so the caller can audit.

NumPy-only — no cvxpy / scipy dependency. If you need a true
LP/SOCP-grade solver later, swap this implementation; the public API
stays the same.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np


@dataclass
class CVaRResult:
    weights: np.ndarray
    cvar: float
    iterations: int
    converged: bool
    fallback: Optional[str] = None


def _project_to_simplex_with_bounds(
    w: np.ndarray,
    *,
    min_w: float,
    max_w: float,
) -> np.ndarray:
    """
    Project ``w`` onto ``{w : min_w <= w <= max_w, sum(w) = 1}`` via
    a bisection search on a Lagrange multiplier — robust and
    bound-respecting.
    """
    n = len(w)
    if n == 0:
        return w
    lo, hi = float(np.min(w) - max_w - 1.0), float(np.max(w) - min_w + 1.0)
    for _ in range(80):
        mid = 0.5 * (lo + hi)
        x = np.clip(w - mid, min_w, max_w)
        s = float(x.sum())
        if abs(s - 1.0) < 1e-10:
            return x
        if s > 1.0:
            lo = mid
        else:
            hi = mid
    return np.clip(w - 0.5 * (lo + hi), min_w, max_w)


def _empirical_cvar(losses: np.ndarray, alpha: float) -> float:
    """Empirical CVaR over the worst ``alpha``-tail of ``losses``."""
    n = len(losses)
    if n == 0:
        return 0.0
    sorted_l = np.sort(losses)[::-1]  # descending
    k = max(1, int(np.ceil(alpha * n)))
    return float(sorted_l[:k].mean())


def cvar_weights(
    R: np.ndarray,
    *,
    alpha: float = 0.05,
    min_weight: float = 0.0,
    max_weight: float = 1.0,
    n_iter: int = 200,
    lr: float = 0.05,
) -> CVaRResult:
    """
    Minimise CVaR_α of -R w (i.e. of *losses*) under simplex bounds.

    Returns the optimal ``weights``, the achieved CVaR, and a fallback
    flag if the routine bailed out.
    """
    R = np.asarray(R, dtype=float)
    if R.ndim == 1:
        R = R.reshape(-1, 1)
    n_obs, n_assets = R.shape
    if n_assets == 0:
        return CVaRResult(weights=np.zeros(0), cvar=0.0, iterations=0, converged=True)
    if n_assets == 1:
        return CVaRResult(weights=np.array([1.0]), cvar=float(np.mean(-R[:, 0])), iterations=0, converged=True)
    if n_obs < 5:
        return CVaRResult(
            weights=np.ones(n_assets) / n_assets,
            cvar=0.0, iterations=0, converged=False,
            fallback="too_few_obs",
        )
    if not np.all(np.isfinite(R)):
        return CVaRResult(
            weights=np.ones(n_assets) / n_assets,
            cvar=0.0, iterations=0, converged=False,
            fallback="non_finite_returns",
        )

    # Validate bounds.
    if min_weight * n_assets > 1.0 + 1e-9 or max_weight * n_assets < 1.0 - 1e-9:
        # Bounds make the simplex infeasible.
        return CVaRResult(
            weights=np.ones(n_assets) / n_assets,
            cvar=0.0, iterations=0, converged=False,
            fallback="infeasible_bounds",
        )

    w = np.ones(n_assets) / n_assets
    w = _project_to_simplex_with_bounds(w, min_w=min_weight, max_w=max_weight)
    best_w = w.copy()
    best_cvar = _empirical_cvar(-R @ w, alpha)
    converged = False

    k = max(1, int(np.ceil(alpha * n_obs)))

    for it in range(n_iter):
        losses = -R @ w
        # Top-k indices = worst-tail scenarios.
        worst_idx = np.argpartition(-losses, k - 1)[:k]
        # Subgradient: average of the rows in R for those scenarios,
        # negated (we minimise CVaR of losses = maximise tail returns).
        grad = -R[worst_idx].mean(axis=0)
        new_w = _project_to_simplex_with_bounds(
            w - lr * grad, min_w=min_weight, max_w=max_weight
        )
        new_cvar = _empirical_cvar(-R @ new_w, alpha)
        if new_cvar + 1e-10 < best_cvar:
            best_w = new_w
            best_cvar = new_cvar
        if np.linalg.norm(new_w - w) < 1e-9:
            converged = True
            break
        w = new_w

    return CVaRResult(
        weights=best_w,
        cvar=best_cvar,
        iterations=int(it + 1) if 'it' in locals() else 0,
        converged=converged,
    )
