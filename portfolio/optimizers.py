"""
portfolio/optimizers.py
=========================
Wave 8 — unified portfolio-optimisation entry point.

Switches between:

- ``equal``  — naive 1/N (always-safe baseline; the fallback)
- ``inverse_variance`` — ``w_i ∝ 1/σ_i²``
- ``hrp``    — Hierarchical Risk Parity (``portfolio/hrp.py``)
- ``cvar``   — CVaR minimisation (``portfolio/cvar.py``)
- ``kelly``  — multi-asset Kelly (``portfolio/kelly.py``)

Every method returns an ``OptimisationResult`` with sum-to-1, non-negative
(except Kelly which honours signed expected returns) weights, a method
name, and a ``fallback`` flag if the routine bailed out.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Optional

import numpy as np
import yaml

from portfolio.covariance import ledoit_wolf_shrinkage, sample_covariance
from portfolio.cvar import cvar_weights
from portfolio.hrp import HRPResult, hrp_weights, inverse_variance_weights
from portfolio.kelly import kelly_weights


DEFAULT_CONFIG_PATH = Path("config/portfolio_optimisation.yaml")


@dataclass
class OptimisationResult:
    weights: np.ndarray
    method: str
    fallback: Optional[str] = None
    diagnostics: dict[str, object] = field(default_factory=dict)


@dataclass
class VolTargetingOverlayConfig:
    enabled: bool = False
    target_vol: float = 0.15
    min_scale: float = 0.25
    max_scale: float = 1.50
    soft_drawdown: float = 0.05
    hard_drawdown: float = 0.20
    drawdown_floor: float = 0.10

    @classmethod
    def from_dict(cls, raw: Optional[Mapping[str, object]]) -> "VolTargetingOverlayConfig":
        if not raw:
            return cls()
        d = dict(raw)
        return cls(
            enabled=bool(d.get("enabled", False)),
            target_vol=float(d.get("target_vol", 0.15)),
            min_scale=float(d.get("min_scale", 0.25)),
            max_scale=float(d.get("max_scale", 1.50)),
            soft_drawdown=float(d.get("soft_drawdown", 0.05)),
            hard_drawdown=float(d.get("hard_drawdown", 0.20)),
            drawdown_floor=float(d.get("drawdown_floor", 0.10)),
        )


@dataclass
class PortfolioOptimisationConfig:
    enabled: bool = False
    method: str = "equal"
    cov_estimator: str = "ledoit_wolf"  # "sample" | "ledoit_wolf"
    min_weight: float = 0.0
    max_weight: float = 1.0
    cvar_alpha: float = 0.05
    kelly_half: bool = True
    kelly_hard_cap: float = 1.0
    kelly_floor: float = 0.0
    fallback_method: str = "equal"
    vol_targeting_overlay: VolTargetingOverlayConfig = field(default_factory=VolTargetingOverlayConfig)

    @classmethod
    def from_dict(cls, raw: Optional[Mapping[str, object]]) -> "PortfolioOptimisationConfig":
        if not raw:
            return cls()
        sect = raw.get("portfolio_optimisation") if "portfolio_optimisation" in raw else raw  # type: ignore[union-attr]
        sect = dict(sect or {})
        return cls(
            enabled=bool(sect.get("enabled", False)),
            method=str(sect.get("method", "equal")),
            cov_estimator=str(sect.get("cov_estimator", "ledoit_wolf")),
            min_weight=float(sect.get("min_weight", 0.0)),
            max_weight=float(sect.get("max_weight", 1.0)),
            cvar_alpha=float(sect.get("cvar_alpha", 0.05)),
            kelly_half=bool(sect.get("kelly_half", True)),
            kelly_hard_cap=float(sect.get("kelly_hard_cap", 1.0)),
            kelly_floor=float(sect.get("kelly_floor", 0.0)),
            fallback_method=str(sect.get("fallback_method", "equal")),
            vol_targeting_overlay=VolTargetingOverlayConfig.from_dict(
                sect.get("vol_targeting_overlay")  # type: ignore[arg-type]
            ),
        )

    @classmethod
    def load(cls, path: Path | str = DEFAULT_CONFIG_PATH) -> "PortfolioOptimisationConfig":
        p = Path(path)
        if not p.exists():
            return cls()
        try:
            raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise RuntimeError(f"could not parse {p}: {exc}") from exc
        return cls.from_dict(raw)


# ── core entry ──────────────────────────────────────────────────────────────


def _equal_weights(n: int) -> np.ndarray:
    if n == 0:
        return np.zeros(0)
    return np.ones(n) / n


def _estimate_cov(returns: np.ndarray, *, estimator: str) -> np.ndarray:
    if estimator == "sample":
        return sample_covariance(returns).matrix
    return ledoit_wolf_shrinkage(returns).matrix


def optimize_weights(
    *,
    returns: Optional[np.ndarray] = None,
    expected_returns: Optional[np.ndarray] = None,
    config: Optional[PortfolioOptimisationConfig] = None,
) -> OptimisationResult:
    """
    Compute target weights for a universe.

    ``returns`` is the historical scenario matrix (``n_obs × n_assets``) —
    required for ``hrp``, ``cvar``, and any covariance-based path.
    ``expected_returns`` is the per-asset expected return vector — used
    by ``kelly``.

    On any failure the routine falls back to ``config.fallback_method``
    (default equal weights) and tags the result.
    """
    cfg = config or PortfolioOptimisationConfig()
    method = (cfg.method or "equal").strip().lower()

    n: int
    if returns is not None:
        R = np.asarray(returns, dtype=float)
        if R.ndim == 1:
            R = R.reshape(-1, 1)
        n = R.shape[1]
    elif expected_returns is not None:
        n = len(expected_returns)
        R = None  # type: ignore[assignment]
    else:
        return OptimisationResult(
            weights=np.zeros(0), method=method, fallback="no_inputs"
        )

    if n == 0:
        return OptimisationResult(weights=np.zeros(0), method=method)
    if n == 1:
        return OptimisationResult(weights=np.array([1.0]), method=method)

    try:
        if method == "equal":
            w = _equal_weights(n)
            return OptimisationResult(weights=w, method="equal")

        if method == "inverse_variance":
            if R is None:
                raise ValueError("inverse_variance requires returns matrix")
            cov = _estimate_cov(R, estimator=cfg.cov_estimator)
            w = inverse_variance_weights(cov)
            return OptimisationResult(
                weights=w, method="inverse_variance",
                diagnostics={"cov_estimator": cfg.cov_estimator},
            )

        if method == "hrp":
            if R is None:
                raise ValueError("hrp requires returns matrix")
            cov = _estimate_cov(R, estimator=cfg.cov_estimator)
            res: HRPResult = hrp_weights(cov)
            return OptimisationResult(
                weights=res.weights,
                method="hrp",
                fallback=res.fallback,
                diagnostics={"ordering": res.ordering, "cov_estimator": cfg.cov_estimator},
            )

        if method == "cvar":
            if R is None:
                raise ValueError("cvar requires returns matrix")
            res2 = cvar_weights(
                R,
                alpha=cfg.cvar_alpha,
                min_weight=cfg.min_weight,
                max_weight=cfg.max_weight,
            )
            return OptimisationResult(
                weights=res2.weights,
                method="cvar",
                fallback=res2.fallback,
                diagnostics={
                    "cvar": float(res2.cvar),
                    "iterations": res2.iterations,
                    "converged": bool(res2.converged),
                },
            )

        if method == "kelly":
            if expected_returns is None:
                raise ValueError("kelly requires expected_returns")
            if R is None:
                # Build identity covariance (no info) — Kelly degrades to mu / 1.
                cov = np.eye(n)
            else:
                cov = _estimate_cov(R, estimator=cfg.cov_estimator)
            ksz = kelly_weights(
                np.asarray(expected_returns, dtype=float),
                cov,
                half_kelly=cfg.kelly_half,
                hard_cap=cfg.kelly_hard_cap,
                floor=cfg.kelly_floor,
            )
            # Normalise *signed* weights: Kelly is signed sizing; for an
            # allocator that wants long-only target weights, we clip to
            # [floor, hard_cap] and renormalise to sum 1 if non-zero.
            w = ksz.weights
            s = float(w.sum())
            if s > 0:
                w = w / s
            else:
                w = _equal_weights(n)
            return OptimisationResult(
                weights=w,
                method="kelly",
                diagnostics={
                    "half_kelly": cfg.kelly_half,
                    "hard_cap": cfg.kelly_hard_cap,
                    "floor": cfg.kelly_floor,
                },
            )

        raise ValueError(f"unknown optimiser method: {cfg.method!r}")

    except Exception as exc:  # noqa: BLE001
        # Any solver failure ⇒ fall back. Never raise from this path —
        # the allocator must always get a feasible weight vector.
        fb = (cfg.fallback_method or "equal").strip().lower()
        if fb == "inverse_variance" and R is not None:
            cov = _estimate_cov(R, estimator=cfg.cov_estimator)
            return OptimisationResult(
                weights=inverse_variance_weights(cov),
                method="inverse_variance",
                fallback=f"primary_failed:{exc.__class__.__name__}",
            )
        return OptimisationResult(
            weights=_equal_weights(n),
            method="equal",
            fallback=f"primary_failed:{exc.__class__.__name__}",
        )
