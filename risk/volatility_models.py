"""
risk/volatility_models.py
==========================
Wave 4 — volatility model family.

Implementations are deliberately NumPy-only so the package imports
without sklearn / arch. The MLE optimisers are coordinate-descent /
random-restart to avoid pulling in scipy. They are good enough for
forecast quality at the timescales mytbot trades (hourly to daily) but
should be replaced with the ``arch`` package's BFGS solvers if it
becomes a runtime dependency in a later wave.

Public API:

- ``ewma_vol(returns, lambda_=0.94)`` → in-sample vol path.
- ``garch11_fit(returns)`` → ``(omega, alpha, beta)``.
- ``garch11_forecast(returns, omega, alpha, beta)`` → 1-step forecast.
- ``gjr_garch_fit(returns)`` → ``(omega, alpha, gamma, beta)``.
- ``gjr_garch_forecast(returns, omega, alpha, gamma, beta)`` → 1-step.
- ``forecast_volatility(returns, model='ewma'|'garch'|'gjr', **kw)`` →
  one-step-ahead annualised vol forecast (assuming daily bars when
  ``annualise=True``; pass ``periods_per_year`` to override).

All functions are leakage-safe: they consume only the past observations
in ``returns`` and never look ahead.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

import numpy as np


# ── EWMA ────────────────────────────────────────────────────────────────────


def ewma_vol(returns: np.ndarray, *, lambda_: float = 0.94) -> np.ndarray:
    """
    RiskMetrics-style EWMA volatility.

    ``sigma_t^2 = lambda_ * sigma_{t-1}^2 + (1 - lambda_) * r_{t-1}^2``

    Returns the in-sample volatility path (same length as ``returns``).
    The first value seeds with the variance of the first 30 (or fewer)
    observations.
    """
    r = np.asarray(returns, dtype=float).ravel()
    n = len(r)
    if n == 0:
        return np.zeros(0)
    out = np.zeros(n)
    seed_n = min(30, n)
    seed_var = float(np.var(r[:seed_n], ddof=1)) if seed_n >= 2 else float(r[0] ** 2)
    out[0] = math.sqrt(max(0.0, seed_var))
    var = seed_var
    for t in range(1, n):
        var = lambda_ * var + (1.0 - lambda_) * (r[t - 1] ** 2)
        out[t] = math.sqrt(max(0.0, var))
    return out


# ── GARCH(1,1) ──────────────────────────────────────────────────────────────


def _garch11_loglik(params: np.ndarray, r: np.ndarray) -> float:
    omega, alpha, beta = float(params[0]), float(params[1]), float(params[2])
    if omega <= 0 or alpha < 0 or beta < 0 or (alpha + beta) >= 1.0:
        return float("inf")
    n = len(r)
    var = max(1e-12, float(np.var(r, ddof=1)))
    ll = 0.0
    for t in range(n):
        var = omega + alpha * (r[t - 1] ** 2 if t > 0 else r[0] ** 2) + beta * var
        if var <= 0 or not math.isfinite(var):
            return float("inf")
        ll += 0.5 * (math.log(2.0 * math.pi * var) + (r[t] ** 2) / var)
    return ll


def _coordinate_descent(
    objective,
    x0: np.ndarray,
    *,
    bounds: list[tuple[float, float]],
    n_iter: int = 80,
    grid: int = 9,
) -> np.ndarray:
    """
    Cheap, dependency-free optimiser. Walks each coordinate over a small
    geometric grid centred on the current value, keeps the best, and
    iterates. Stops early when no coordinate improves.
    """
    x = np.array(x0, dtype=float)
    best = float(objective(x))
    for _ in range(n_iter):
        improved = False
        for i in range(len(x)):
            lo, hi = bounds[i]
            cur = x[i]
            radius = max(abs(cur) * 0.5, 1e-3)
            candidates = np.linspace(max(lo, cur - radius), min(hi, cur + radius), grid)
            for c in candidates:
                trial = x.copy()
                trial[i] = c
                val = float(objective(trial))
                if val < best - 1e-9:
                    best = val
                    x = trial
                    improved = True
        if not improved:
            break
    return x


def garch11_fit(returns: np.ndarray) -> tuple[float, float, float]:
    """Fit GARCH(1,1) by Gaussian MLE. Returns ``(omega, alpha, beta)``."""
    r = np.asarray(returns, dtype=float).ravel()
    if len(r) < 30:
        # Not enough data — return a degenerate constant-variance "model".
        sigma2 = max(1e-12, float(np.var(r, ddof=1) if len(r) >= 2 else r.var()))
        return (sigma2, 0.0, 0.0)
    var = max(1e-10, float(np.var(r, ddof=1)))
    x0 = np.array([0.05 * var, 0.05, 0.90])
    bounds = [(1e-12, 10.0 * var), (0.0, 0.99), (0.0, 0.999)]
    x = _coordinate_descent(lambda p: _garch11_loglik(p, r), x0, bounds=bounds)
    return (float(x[0]), float(x[1]), float(x[2]))


def garch11_forecast(
    returns: np.ndarray, omega: float, alpha: float, beta: float
) -> float:
    """One-step variance forecast given fitted parameters."""
    r = np.asarray(returns, dtype=float).ravel()
    if len(r) == 0:
        return omega / max(1e-12, 1.0 - alpha - beta)
    # Reconstruct the conditional variance trajectory then step one ahead.
    var = max(1e-12, float(np.var(r, ddof=1)))
    for t in range(len(r)):
        var = omega + alpha * (r[t - 1] ** 2 if t > 0 else r[0] ** 2) + beta * var
    forecast = omega + alpha * (r[-1] ** 2) + beta * var
    return max(0.0, float(forecast))


# ── GJR-GARCH (asymmetric) ──────────────────────────────────────────────────


def _gjr_loglik(params: np.ndarray, r: np.ndarray) -> float:
    omega, alpha, gamma, beta = (float(params[0]), float(params[1]), float(params[2]), float(params[3]))
    if omega <= 0 or alpha < 0 or gamma < 0 or beta < 0:
        return float("inf")
    if (alpha + 0.5 * gamma + beta) >= 1.0:
        return float("inf")
    n = len(r)
    var = max(1e-12, float(np.var(r, ddof=1)))
    ll = 0.0
    for t in range(n):
        prev_r = r[t - 1] if t > 0 else r[0]
        leverage = (prev_r ** 2) if prev_r < 0 else 0.0
        var = omega + alpha * (prev_r ** 2) + gamma * leverage + beta * var
        if var <= 0 or not math.isfinite(var):
            return float("inf")
        ll += 0.5 * (math.log(2.0 * math.pi * var) + (r[t] ** 2) / var)
    return ll


def gjr_garch_fit(returns: np.ndarray) -> tuple[float, float, float, float]:
    r = np.asarray(returns, dtype=float).ravel()
    if len(r) < 30:
        sigma2 = max(1e-12, float(np.var(r, ddof=1) if len(r) >= 2 else r.var()))
        return (sigma2, 0.0, 0.0, 0.0)
    var = max(1e-10, float(np.var(r, ddof=1)))
    x0 = np.array([0.05 * var, 0.05, 0.05, 0.85])
    bounds = [(1e-12, 10.0 * var), (0.0, 0.5), (0.0, 0.5), (0.0, 0.99)]
    x = _coordinate_descent(lambda p: _gjr_loglik(p, r), x0, bounds=bounds)
    return (float(x[0]), float(x[1]), float(x[2]), float(x[3]))


def gjr_garch_forecast(
    returns: np.ndarray, omega: float, alpha: float, gamma: float, beta: float
) -> float:
    r = np.asarray(returns, dtype=float).ravel()
    if len(r) == 0:
        denom = max(1e-12, 1.0 - alpha - 0.5 * gamma - beta)
        return omega / denom
    var = max(1e-12, float(np.var(r, ddof=1)))
    for t in range(len(r)):
        prev_r = r[t - 1] if t > 0 else r[0]
        lev = (prev_r ** 2) if prev_r < 0 else 0.0
        var = omega + alpha * (prev_r ** 2) + gamma * lev + beta * var
    last = r[-1]
    lev = (last ** 2) if last < 0 else 0.0
    forecast = omega + alpha * (last ** 2) + gamma * lev + beta * var
    return max(0.0, float(forecast))


# ── unified forecaster ──────────────────────────────────────────────────────


@dataclass
class VolForecast:
    model: str
    one_step_variance: float
    one_step_vol: float
    annualised_vol: float
    params: dict[str, float]


def forecast_volatility(
    returns: np.ndarray,
    *,
    model: str = "ewma",
    annualise: bool = True,
    periods_per_year: int = 252,
    lambda_: float = 0.94,
) -> Optional[VolForecast]:
    """
    Fit-and-forecast helper for the runtime path.

    Returns ``None`` when ``returns`` is too short to fit the chosen
    model. A Wave 4 caller (e.g. allocator or strategy) should treat
    that as "use the existing heuristic vol".
    """
    r = np.asarray(returns, dtype=float).ravel()
    if len(r) < 5:
        return None
    m = (model or "ewma").strip().lower()

    if m == "ewma":
        path = ewma_vol(r, lambda_=lambda_)
        var = float(path[-1] ** 2) if len(path) else 0.0
        params = {"lambda": float(lambda_)}
    elif m in ("garch", "garch11", "garch(1,1)"):
        omega, alpha, beta = garch11_fit(r)
        var = garch11_forecast(r, omega, alpha, beta)
        params = {"omega": omega, "alpha": alpha, "beta": beta}
    elif m in ("gjr", "gjr_garch", "gjr-garch"):
        omega, alpha, gamma, beta = gjr_garch_fit(r)
        var = gjr_garch_forecast(r, omega, alpha, gamma, beta)
        params = {"omega": omega, "alpha": alpha, "gamma": gamma, "beta": beta}
    else:
        raise ValueError(f"unknown vol model: {model!r}")

    vol = math.sqrt(max(0.0, var))
    annual = vol * math.sqrt(periods_per_year) if annualise else vol
    return VolForecast(
        model=m,
        one_step_variance=float(var),
        one_step_vol=float(vol),
        annualised_vol=float(annual),
        params=params,
    )
