"""
tests/test_wave8_portfolio_optimisation.py
=============================================
Wave 8 acceptance tests for the portfolio-optimisation modules.

Coverage:

- ``kelly_fraction`` clips to [floor, hard_cap], handles bad inputs.
- ``kelly_weights`` keeps gross exposure inside the hard cap.
- ``vol_scale`` clips to [min_scale, max_scale], 1.0 on bad inputs.
- ``drawdown_scale`` linearly interpolates floor as drawdown grows.
- ``hrp_weights`` produces non-negative weights summing to 1; falls
  back gracefully on singular / non-finite covariance.
- HRP recovers ~equal weights for an equicorrelated covariance.
- ``cvar_weights`` returns a feasible weight vector and lowers CVaR
  vs equal weights on a synthetic two-asset case.
- ``optimize_weights`` switches by config and produces feasible
  output for every supported method.
- Optimiser failure path falls back to ``fallback_method``.
- YAML config round-trip.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from portfolio.cvar import cvar_weights
from portfolio.hrp import hrp_weights, inverse_variance_weights
from portfolio.kelly import (
    DEFAULT_HARD_CAP,
    cap_to_safety_bounds,
    kelly_fraction,
    kelly_weights,
)
from portfolio.optimizers import (
    PortfolioOptimisationConfig,
    optimize_weights,
)
from portfolio.vol_targeting import (
    combined_scale,
    drawdown_scale,
    vol_scale,
)


# ── kelly ───────────────────────────────────────────────────────────────────


def test_kelly_fraction_clips_to_bounds() -> None:
    # Huge edge / tiny vol ⇒ hard cap.
    assert kelly_fraction(0.5, 0.01) == DEFAULT_HARD_CAP
    # No edge ⇒ floor.
    assert kelly_fraction(0.0, 0.01) == 0.0
    # Negative edge with floor=0 ⇒ floor.
    assert kelly_fraction(-0.5, 0.01, floor=0.0) == 0.0
    # Negative edge with floor=-cap ⇒ -cap.
    assert kelly_fraction(-0.5, 0.01, floor=-1.0, hard_cap=1.0) == -1.0


def test_kelly_fraction_bad_inputs_return_zero() -> None:
    assert kelly_fraction(0.1, 0.0) == 0.0
    assert kelly_fraction(0.1, -0.5) == 0.0
    assert kelly_fraction(float("nan"), 0.01) == 0.0


def test_kelly_fraction_half_kelly_is_half() -> None:
    full = kelly_fraction(0.04, 0.04, half_kelly=False, hard_cap=10.0)
    half = kelly_fraction(0.04, 0.04, half_kelly=True, hard_cap=10.0)
    assert abs(half - 0.5 * full) < 1e-12


def test_kelly_weights_gross_within_cap() -> None:
    mu = np.array([0.02, 0.03, 0.01, 0.04])
    cov = np.diag([0.01, 0.02, 0.005, 0.03])
    out = kelly_weights(mu, cov, half_kelly=True, hard_cap=0.5, floor=0.0)
    assert np.all(out.weights >= -1e-12)
    assert float(np.sum(np.abs(out.weights))) <= 0.5 + 1e-9


def test_cap_to_safety_bounds() -> None:
    assert cap_to_safety_bounds(0.7, hard_cap=0.5) == 0.5
    assert cap_to_safety_bounds(-0.1, floor=0.0) == 0.0
    assert cap_to_safety_bounds(0.3) == 0.3  # no cap, no floor


# ── vol targeting ──────────────────────────────────────────────────────────


def test_vol_scale_clips() -> None:
    assert vol_scale(target_vol=0.10, realised_vol=0.10) == pytest.approx(1.0)
    # Realised << target ⇒ scale up; clipped to max_scale.
    assert vol_scale(target_vol=0.10, realised_vol=0.01, max_scale=2.0) == 2.0
    # Realised >> target ⇒ scale down; clipped to min_scale.
    assert vol_scale(target_vol=0.10, realised_vol=10.0, min_scale=0.1) == 0.1


def test_vol_scale_bad_inputs() -> None:
    assert vol_scale(target_vol=0.10, realised_vol=0.0) == 1.0
    assert vol_scale(target_vol=0.10, realised_vol=float("nan")) == 1.0
    assert vol_scale(target_vol=0.0, realised_vol=0.10) == 1.0


def test_drawdown_scale_linear() -> None:
    assert drawdown_scale(drawdown=0.0) == 1.0
    assert drawdown_scale(drawdown=0.05) == 1.0  # at soft threshold
    assert drawdown_scale(drawdown=0.20) == pytest.approx(0.10)  # at hard, floor
    mid = drawdown_scale(drawdown=0.125)
    assert 0.10 < mid < 1.0


def test_combined_scale_multiplies() -> None:
    res = combined_scale(target_vol=0.10, realised_vol=0.05, drawdown=0.0)
    assert res.scale == pytest.approx(2.0)  # vol ×2, dd ×1
    res2 = combined_scale(target_vol=0.10, realised_vol=0.05, drawdown=0.20, floor=0.10)
    # vol × dd = 2.0 * 0.10 = 0.20
    assert res2.scale == pytest.approx(0.20)


# ── HRP ────────────────────────────────────────────────────────────────────


def _toy_returns(n: int = 200, p: int = 4, seed: int = 0):
    rng = np.random.default_rng(seed)
    return rng.normal(0, 0.01, (n, p))


def test_hrp_weights_sum_to_one_nonneg() -> None:
    R = _toy_returns(seed=1)
    cov = np.cov(R, rowvar=False, ddof=1)
    res = hrp_weights(cov)
    assert np.all(res.weights >= -1e-12)
    assert float(np.sum(res.weights)) == pytest.approx(1.0, abs=1e-9)
    assert res.fallback is None
    assert len(res.ordering) == cov.shape[0]


def test_hrp_singular_falls_back_to_equal() -> None:
    p = 4
    cov = np.zeros((p, p))  # singular
    res = hrp_weights(cov)
    assert res.fallback in {"singular", "non_finite_cov"}
    assert float(np.sum(res.weights)) == pytest.approx(1.0)


def test_hrp_equicorrelated_close_to_equal() -> None:
    rho = 0.3
    sd = np.array([0.01, 0.01, 0.01, 0.01])
    p = len(sd)
    rho_mat = rho * np.ones((p, p))
    np.fill_diagonal(rho_mat, 1.0)
    cov = rho_mat * np.outer(sd, sd)
    res = hrp_weights(cov)
    expected = 1.0 / p
    assert np.all(np.abs(res.weights - expected) < 0.05)


def test_hrp_single_asset_returns_one() -> None:
    res = hrp_weights(np.array([[0.01]]))
    np.testing.assert_allclose(res.weights, [1.0])


def test_inverse_variance_low_vol_gets_more() -> None:
    cov = np.diag([0.01, 0.04])
    w = inverse_variance_weights(cov)
    assert w[0] > w[1]
    assert float(w.sum()) == pytest.approx(1.0)


# ── CVaR ───────────────────────────────────────────────────────────────────


def test_cvar_weights_feasible_and_better_than_equal() -> None:
    rng = np.random.default_rng(2)
    n = 250
    # Asset 0 has fat negative tail; asset 1 is benign. Optimiser should
    # tilt away from asset 0 to lower CVaR.
    a0 = np.where(rng.random(n) < 0.05, -0.10, rng.normal(0.001, 0.01, n))
    a1 = rng.normal(0.001, 0.01, n)
    R = np.column_stack([a0, a1])
    res = cvar_weights(R, alpha=0.05, n_iter=300, lr=0.05)
    assert float(np.sum(res.weights)) == pytest.approx(1.0, abs=1e-6)
    assert np.all(res.weights >= -1e-9)
    # Optimised CVaR ≤ equal-weight CVaR.
    eq_w = np.array([0.5, 0.5])
    losses_eq = -R @ eq_w
    sorted_l = np.sort(losses_eq)[::-1]
    k = max(1, int(np.ceil(0.05 * n)))
    cvar_eq = float(sorted_l[:k].mean())
    assert res.cvar <= cvar_eq + 1e-6
    # And the tilt should be away from the fat-tail asset.
    assert res.weights[1] >= res.weights[0]


def test_cvar_handles_too_few_observations() -> None:
    R = np.array([[0.01, 0.02], [-0.01, 0.0]])
    res = cvar_weights(R)
    assert res.fallback == "too_few_obs"
    assert float(np.sum(res.weights)) == pytest.approx(1.0)


def test_cvar_handles_non_finite() -> None:
    R = np.full((50, 3), np.nan)
    res = cvar_weights(R)
    assert res.fallback == "non_finite_returns"


# ── unified optimizer ──────────────────────────────────────────────────────


def test_optimize_weights_equal() -> None:
    R = _toy_returns(p=5, seed=3)
    cfg = PortfolioOptimisationConfig(method="equal")
    res = optimize_weights(returns=R, config=cfg)
    np.testing.assert_allclose(res.weights, np.ones(5) / 5)


def test_optimize_weights_inverse_variance() -> None:
    R = _toy_returns(p=4, seed=4)
    cfg = PortfolioOptimisationConfig(method="inverse_variance")
    res = optimize_weights(returns=R, config=cfg)
    assert float(res.weights.sum()) == pytest.approx(1.0)


def test_optimize_weights_hrp() -> None:
    R = _toy_returns(p=6, seed=5)
    cfg = PortfolioOptimisationConfig(method="hrp")
    res = optimize_weights(returns=R, config=cfg)
    assert float(res.weights.sum()) == pytest.approx(1.0, abs=1e-9)
    assert np.all(res.weights >= 0)


def test_optimize_weights_cvar_simplex() -> None:
    R = _toy_returns(p=3, seed=6) * 2
    cfg = PortfolioOptimisationConfig(method="cvar", cvar_alpha=0.1)
    res = optimize_weights(returns=R, config=cfg)
    assert float(res.weights.sum()) == pytest.approx(1.0, abs=1e-6)
    assert np.all(res.weights >= -1e-9)


def test_optimize_weights_kelly_uses_expected_returns() -> None:
    # Use a synthetic returns matrix with realistic variance so the
    # raw Kelly weights don't all saturate at the hard cap.
    rng = np.random.default_rng(7)
    R = rng.normal(0, 0.2, (200, 3))  # ~annualised vol 0.20
    mu = np.array([0.02, 0.0, 0.04])
    cfg = PortfolioOptimisationConfig(method="kelly", kelly_half=True, kelly_hard_cap=0.6)
    res = optimize_weights(returns=R, expected_returns=mu, config=cfg)
    assert float(res.weights.sum()) == pytest.approx(1.0, abs=1e-9)
    # Best-performing asset (index 2, mu=0.04) should get the largest share.
    assert int(np.argmax(res.weights)) == 2
    # Best > worst.
    assert res.weights[2] > res.weights[1]


def test_optimize_weights_unknown_method_falls_back(monkeypatch) -> None:
    R = _toy_returns(p=3, seed=8)
    cfg = PortfolioOptimisationConfig(method="banana", fallback_method="equal")
    res = optimize_weights(returns=R, config=cfg)
    np.testing.assert_allclose(res.weights, np.ones(3) / 3)
    assert res.method == "equal"
    assert res.fallback is not None and res.fallback.startswith("primary_failed")


def test_optimize_weights_no_inputs_returns_empty() -> None:
    cfg = PortfolioOptimisationConfig(method="hrp")
    res = optimize_weights(config=cfg)
    assert len(res.weights) == 0
    assert res.fallback == "no_inputs"


# ── config ─────────────────────────────────────────────────────────────────


def test_default_yaml_loads_disabled() -> None:
    cfg = PortfolioOptimisationConfig.load(Path("config/portfolio_optimisation.yaml"))
    assert cfg.enabled is False
    assert cfg.method == "hrp"


def test_config_from_dict_overrides() -> None:
    cfg = PortfolioOptimisationConfig.from_dict(
        {"portfolio_optimisation": {"enabled": True, "method": "cvar", "cvar_alpha": 0.10}}
    )
    assert cfg.enabled is True
    assert cfg.method == "cvar"
    assert cfg.cvar_alpha == 0.10
