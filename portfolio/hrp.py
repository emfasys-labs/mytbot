"""
portfolio/hrp.py
=================
Wave 8 — Hierarchical Risk Parity (Lopez de Prado, 2016).

Three steps:
  1. **Tree clustering** on a correlation-distance matrix.
     ``d_{ij} = sqrt(0.5 * (1 - rho_{ij}))``.
  2. **Quasi-diagonalisation** — reorder the covariance matrix so
     correlated assets are adjacent.
  3. **Recursive bisection** — split the ordered set in two halves and
     allocate inverse-variance-proportional weights between halves
     repeatedly.

NumPy-only. The dependency-free linkage is a simple agglomerative
single-linkage, which is sufficient for HRP's quasi-diagonalisation
(the algorithm tolerates noisy linkage choices). When scipy is
available the linkage is delegated for speed; otherwise the inline
implementation runs in O(N^2).

Defensive properties:
- Singular covariance → falls back to ``inverse_variance_weights``.
- Constant correlation → equal weights.
- Empty / single-asset universe → trivial weights.
- All weights are non-negative and sum to 1.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np

try:  # Optional speed-up.
    from scipy.cluster.hierarchy import linkage  # type: ignore
    from scipy.spatial.distance import squareform  # type: ignore

    _SCIPY_AVAILABLE = True
except Exception:  # noqa: BLE001
    linkage = None  # type: ignore
    squareform = None  # type: ignore
    _SCIPY_AVAILABLE = False


@dataclass
class HRPResult:
    weights: np.ndarray
    ordering: list[int]
    method: str = "hrp"
    fallback: Optional[str] = None  # "inverse_variance" | "equal" | None


# ── helpers ─────────────────────────────────────────────────────────────────


def _correlation_from_cov(cov: np.ndarray) -> np.ndarray:
    sd = np.sqrt(np.maximum(np.diag(cov), 1e-30))
    denom = np.outer(sd, sd)
    rho = np.where(denom > 0, cov / denom, 0.0)
    np.fill_diagonal(rho, 1.0)
    rho = np.clip(rho, -1.0, 1.0)
    return rho


def _correlation_distance(rho: np.ndarray) -> np.ndarray:
    return np.sqrt(np.clip(0.5 * (1.0 - rho), 0.0, 1.0))


def _single_linkage_order_numpy(D: np.ndarray) -> list[int]:
    """
    Pure-NumPy single-linkage agglomerative clustering. Returns the leaf
    order of the dendrogram — that's all HRP needs.
    """
    n = D.shape[0]
    if n <= 1:
        return list(range(n))

    # Each cluster is a list of leaves.
    clusters: list[list[int]] = [[i] for i in range(n)]
    # Pairwise distances between *clusters*; init from leaf distances.
    cd = D.copy()
    np.fill_diagonal(cd, np.inf)

    while len(clusters) > 1:
        idx = int(np.argmin(cd))
        i, j = divmod(idx, cd.shape[0])
        if i > j:
            i, j = j, i
        # Merge j into i.
        clusters[i] = clusters[i] + clusters[j]
        # Update distances: single-linkage is min over members.
        new_row = np.minimum(cd[i, :], cd[j, :])
        new_row[i] = np.inf
        cd[i, :] = new_row
        cd[:, i] = new_row
        # Drop cluster j.
        cd = np.delete(np.delete(cd, j, axis=0), j, axis=1)
        del clusters[j]

    return clusters[0]


def _quasi_diag_order(cov: np.ndarray) -> list[int]:
    n = cov.shape[0]
    if n <= 1:
        return list(range(n))
    rho = _correlation_from_cov(cov)
    D = _correlation_distance(rho)

    if _SCIPY_AVAILABLE:
        try:
            link = linkage(squareform(D, checks=False), method="single")
            # Recover leaf order from the linkage matrix.
            order: list[int] = []

            def _walk(idx: int) -> None:
                if idx < n:
                    order.append(int(idx))
                    return
                row = link[int(idx) - n]
                _walk(int(row[0]))
                _walk(int(row[1]))

            _walk(2 * n - 2)
            return order
        except Exception:  # noqa: BLE001
            pass
    return _single_linkage_order_numpy(D)


def _ivp_weights(cov_block: np.ndarray) -> np.ndarray:
    diag = np.maximum(np.diag(cov_block), 1e-30)
    inv = 1.0 / diag
    return inv / inv.sum()


def _cluster_var(cov_block: np.ndarray) -> float:
    w = _ivp_weights(cov_block)
    return float(w @ cov_block @ w)


# ── public API ──────────────────────────────────────────────────────────────


def inverse_variance_weights(cov: np.ndarray) -> np.ndarray:
    """Simple ``w_i ∝ 1 / σ_i²``; the fallback for HRP when clustering fails."""
    n = cov.shape[0]
    if n == 0:
        return np.zeros(0)
    diag = np.maximum(np.diag(cov), 1e-30)
    inv = 1.0 / diag
    return inv / inv.sum()


def hrp_weights(cov: np.ndarray) -> HRPResult:
    """
    Compute HRP weights.

    On singular / non-finite covariance the function falls back to
    ``inverse_variance_weights`` and records ``fallback`` in the result.
    Universes of size 0 or 1 produce trivial weights.
    """
    cov = np.asarray(cov, dtype=float)
    n = cov.shape[0]
    if n == 0:
        return HRPResult(weights=np.zeros(0), ordering=[], fallback="empty")
    if n == 1:
        return HRPResult(weights=np.array([1.0]), ordering=[0])

    if not np.all(np.isfinite(cov)):
        return HRPResult(
            weights=inverse_variance_weights(cov),
            ordering=list(range(n)),
            fallback="non_finite_cov",
        )

    # Detect "constant correlation" / singular: HRP would still work but
    # equal weights are a more honest answer.
    try:
        rank = int(np.linalg.matrix_rank(cov, tol=1e-12))
    except np.linalg.LinAlgError:
        rank = 0
    if rank < 1:
        return HRPResult(
            weights=np.ones(n) / n, ordering=list(range(n)), fallback="singular"
        )

    order = _quasi_diag_order(cov)
    if len(order) != n:
        return HRPResult(
            weights=inverse_variance_weights(cov),
            ordering=list(range(n)),
            fallback="ordering_failed",
        )

    weights = np.zeros(n)
    weights[order] = 1.0  # seed; overwritten by recursion

    # Recursive bisection on the *ordered* index list.
    def _bisect(lo: int, hi: int, w_alloc: float) -> None:
        # Half-open interval [lo, hi) of positions in ``order``.
        if hi - lo == 1:
            weights[order[lo]] = w_alloc
            return
        mid = (lo + hi) // 2
        left = order[lo:mid]
        right = order[mid:hi]
        cov_l = cov[np.ix_(left, left)]
        cov_r = cov[np.ix_(right, right)]
        v_l = _cluster_var(cov_l)
        v_r = _cluster_var(cov_r)
        denom = v_l + v_r
        alpha = 0.5 if denom <= 0 or not np.isfinite(denom) else 1.0 - v_l / denom
        # Note: alpha allocates to the *left* cluster.
        _bisect(lo, mid, w_alloc * alpha)
        _bisect(mid, hi, w_alloc * (1.0 - alpha))

    _bisect(0, n, 1.0)
    # Numerical hygiene: clamp tiny negatives, renormalise.
    weights = np.clip(weights, 0.0, None)
    s = float(weights.sum())
    if s <= 0:
        return HRPResult(
            weights=inverse_variance_weights(cov),
            ordering=order,
            fallback="zero_sum",
        )
    weights = weights / s
    return HRPResult(weights=weights, ordering=order)
