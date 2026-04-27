"""
tests/test_wave3_factor_sleeve.py
==================================
Wave 3 acceptance tests for the cross-sectional factor sleeve.

Coverage:

1. Price-based factors compute the right values on a known synthetic
   series and return ``None`` when the window is too short.
2. Fundamental factors gracefully handle missing / non-positive inputs.
3. ``rank_cross_section`` produces zero-mean unit-variance z-scores
   (within finite tolerance) and ``neutralise_by_group`` removes the
   group mean.
4. ``composite_factor_score`` blends families with the right sign and
   honours the ``treat_missing`` setting.
5. ``FactorSleeve`` is OFF by default and emits no candidates.
6. When enabled, the sleeve picks the top-N composite scorers as longs
   and tags ``factor_*`` metadata on every candidate.
7. ``FactorSleeveConfig.load`` round-trips the YAML.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from data.factor_features import (
    beta_to_benchmark,
    build_price_factors,
    drawdown_stability,
    momentum_12_1,
    realised_vol,
    reversal_1m,
)
from data.fundamental_features import (
    accruals_proxy,
    build_fundamental_factors,
    earnings_yield,
    leverage,
    profitability,
)
from signals.factor_scoring import (
    DEFAULT_BLEND,
    FactorBlend,
    FactorFamily,
    FactorWeight,
    composite_factor_score,
    neutralise_by_group,
    rank_cross_section,
)
from strategies.factor_sleeve import (
    FactorSleeve,
    FactorSleeveConfig,
    FactorUniverseInput,
)


# ── synthetic helpers ───────────────────────────────────────────────────────


def _make_series(n: int = 300, drift: float = 0.0005, vol: float = 0.01, seed: int = 0):
    rng = np.random.default_rng(seed)
    rets = rng.normal(drift, vol, n)
    px = 100.0 * np.exp(np.cumsum(rets))
    idx = pd.date_range("2025-01-01", periods=n, freq="D", tz="UTC")
    return pd.Series(px, index=idx, name="close")


# ── 1. price factors ────────────────────────────────────────────────────────


def test_momentum_12_1_too_short_returns_none() -> None:
    s = _make_series(n=100)
    assert momentum_12_1(s) is None


def test_momentum_12_1_returns_finite_on_long_series() -> None:
    s = _make_series(n=300, drift=0.001, seed=1)
    val = momentum_12_1(s)
    assert val is not None and math.isfinite(val)


def test_reversal_1m_short_window() -> None:
    s = _make_series(n=10)
    assert reversal_1m(s) is None
    s2 = _make_series(n=40)
    assert reversal_1m(s2) is not None


def test_realised_vol_positive_for_volatile_series() -> None:
    s = _make_series(n=200, vol=0.02, seed=2)
    rv = realised_vol(s)
    assert rv is not None and rv > 0


def test_drawdown_stability_close_to_one_for_smooth_series() -> None:
    s = pd.Series(np.linspace(100, 110, 252), index=pd.date_range("2025-01-01", periods=252))
    score = drawdown_stability(s)
    assert score is not None and score > 0.99


def test_beta_recovers_unit_for_self() -> None:
    s = _make_series(n=300, seed=3)
    b = beta_to_benchmark(s, s)
    assert b is not None and abs(b - 1.0) < 1e-9


def test_build_price_factors_handles_short_history_gracefully() -> None:
    s = _make_series(n=20)
    out = build_price_factors(s)
    # Most factors return None on short input; nothing should raise.
    assert isinstance(out, dict)
    assert "momentum_12_1" in out


# ── 2. fundamental factors ──────────────────────────────────────────────────


def test_earnings_yield_basic() -> None:
    assert earnings_yield({"eps_ttm": 5.0, "price": 100.0}) == pytest.approx(0.05)


def test_earnings_yield_missing_returns_none() -> None:
    assert earnings_yield({}) is None
    assert earnings_yield({"eps_ttm": 5.0}) is None
    assert earnings_yield({"eps_ttm": 5.0, "price": 0}) is None
    assert earnings_yield({"eps_ttm": 5.0, "price": -1}) is None


def test_leverage_returns_none_on_zero_equity() -> None:
    assert leverage({"total_debt": 100.0, "total_equity": 0.0}) is None


def test_profitability_basic() -> None:
    assert profitability({"operating_income": 20.0, "total_assets": 200.0}) == pytest.approx(0.10)


def test_accruals_proxy_signed_correctly() -> None:
    # NI > CFO ⇒ positive accruals (lower quality).
    v = accruals_proxy({"net_income": 50, "operating_cash_flow": 30, "total_assets": 1000})
    assert v == pytest.approx(0.02)
    # NI < CFO ⇒ negative accruals (higher quality).
    v2 = accruals_proxy({"net_income": 30, "operating_cash_flow": 50, "total_assets": 1000})
    assert v2 == pytest.approx(-0.02)


def test_build_fundamental_factors_all_keys_present() -> None:
    out = build_fundamental_factors(None)
    expected = {
        "earnings_yield",
        "book_to_market",
        "fcf_yield",
        "sales_yield",
        "profitability",
        "margin_stability",
        "leverage",
        "accruals_proxy",
        "dividend_yield",
        "fx_carry",
        "crypto_funding_carry",
        "bond_yield_carry",
    }
    assert set(out.keys()) == expected
    # All None when no input.
    assert all(v is None for v in out.values())


# ── 3. ranking + neutralisation ─────────────────────────────────────────────


def test_rank_cross_section_zero_mean_unit_variance() -> None:
    vals = {"A": 1.0, "B": 2.0, "C": 3.0, "D": 4.0, "E": 5.0}
    z = rank_cross_section(vals)
    arr = [v for v in z.values() if v is not None]
    assert len(arr) == 5
    assert abs(sum(arr) / len(arr)) < 1e-9
    var = sum((x - sum(arr) / len(arr)) ** 2 for x in arr) / (len(arr) - 1)
    assert abs(var - 1.0) < 1e-9


def test_rank_cross_section_keeps_none_entries() -> None:
    vals = {"A": 1.0, "B": None, "C": 3.0}
    z = rank_cross_section(vals)
    assert z["B"] is None
    assert z["A"] is not None


def test_rank_cross_section_handles_constant_input() -> None:
    z = rank_cross_section({"A": 1.0, "B": 1.0})
    assert z == {"A": 0.0, "B": 0.0}


def test_neutralise_by_group_subtracts_group_mean() -> None:
    vals = {"A": 1.0, "B": 3.0, "C": 10.0, "D": 14.0}
    groups = {"A": "x", "B": "x", "C": "y", "D": "y"}
    out = neutralise_by_group(vals, groups=groups)
    # Group x mean = 2 → A=-1, B=+1; group y mean = 12 → C=-2, D=+2.
    assert out["A"] == pytest.approx(-1.0)
    assert out["B"] == pytest.approx(1.0)
    assert out["C"] == pytest.approx(-2.0)
    assert out["D"] == pytest.approx(2.0)


def test_neutralise_singleton_group_unchanged() -> None:
    vals = {"A": 1.0, "B": 3.0, "C": 10.0}
    groups = {"A": "x", "B": "x", "C": "y"}  # y has only 1 finite value
    out = neutralise_by_group(vals, groups=groups)
    assert out["C"] == 10.0  # unchanged


# ── 4. composite blending ──────────────────────────────────────────────────


def test_composite_score_respects_factor_sign() -> None:
    # Two symbols, one factor, "lower is better" (weight -1).
    blend = FactorBlend(
        families=(
            FactorFamily(
                name="risk",
                weight=1.0,
                members=(FactorWeight("realised_vol", -1.0),),
            ),
        )
    )
    per_sym = {"low_vol": {"realised_vol": 0.1}, "high_vol": {"realised_vol": 0.5}}
    scores = composite_factor_score(per_symbol_factors=per_sym, blend=blend)
    assert scores.composite["low_vol"] > scores.composite["high_vol"]


def test_composite_score_drop_excludes_partial_symbols() -> None:
    blend = FactorBlend(
        families=(
            FactorFamily(
                name="dummy",
                weight=1.0,
                members=(
                    FactorWeight("a", 1.0),
                    FactorWeight("b", 1.0),
                ),
            ),
        )
    )
    per_sym = {
        "full": {"a": 1.0, "b": 1.0},
        "partial": {"a": 1.0, "b": None},
    }
    scores = composite_factor_score(
        per_symbol_factors=per_sym, blend=blend, treat_missing="drop"
    )
    assert "partial" not in scores.composite
    assert "full" in scores.composite


def test_composite_score_with_default_blend_runs_on_random_universe() -> None:
    rng = np.random.default_rng(0)
    universe = {f"S{i}": {} for i in range(20)}
    for sym in universe:
        for k in (
            "earnings_yield",
            "book_to_market",
            "profitability",
            "leverage",
            "momentum_12_1",
            "realised_vol",
        ):
            universe[sym][k] = float(rng.normal())
    scores = composite_factor_score(per_symbol_factors=universe, blend=DEFAULT_BLEND)
    assert len(scores.composite) == 20
    # Family breakdown matches the blend.
    assert set(scores.by_family.keys()) == {"value", "quality", "momentum", "defensive", "carry"}


# ── 5. sleeve OFF by default ────────────────────────────────────────────────


def test_factor_sleeve_disabled_emits_nothing() -> None:
    sleeve = FactorSleeve()
    rows = [
        FactorUniverseInput(symbol="A", asset_class="equity", close=_make_series()),
        FactorUniverseInput(symbol="B", asset_class="equity", close=_make_series(seed=1)),
    ]
    cands, scores = sleeve.evaluate(rows)
    assert cands == []
    assert scores.composite == {}


def test_factor_sleeve_default_yaml_loads_enabled_for_paper() -> None:
    cfg = FactorSleeveConfig.load(Path("config/factor_sleeve.yaml"))
    assert cfg.enabled is True
    # Make sure the blend parses without error.
    assert any(fam.name == "value" for fam in cfg.blend.families)


# ── 6. sleeve emission when enabled ─────────────────────────────────────────


def test_factor_sleeve_emits_long_top_n_when_enabled() -> None:
    cfg = FactorSleeveConfig(
        enabled=True,
        long_top_n=2,
        short_bottom_n=0,
        neutralise_by_asset_class=False,
        treat_missing="zero",
    )
    sleeve = FactorSleeve(cfg)
    rows = [
        FactorUniverseInput(
            symbol=f"S{i}",
            asset_class="equity",
            close=_make_series(drift=d, seed=i),
        )
        for i, d in enumerate([0.002, 0.001, 0.0005, 0.0, -0.0005])
    ]
    cands, scores = sleeve.evaluate(rows)
    assert len(cands) == 2
    assert all(c.side == "long" for c in cands)
    # Each candidate carries factor metadata for the dashboard.
    for c in cands:
        assert c.metadata.get("factor_sleeve") is True
        assert "factor_composite_z" in c.metadata
        assert "factor_family_breakdown" in c.metadata
        assert c.strategy_name == "factor_sleeve"
    # Composite scores cover the universe.
    assert len(scores.composite) == 5


def test_factor_sleeve_emits_short_bottom_when_configured() -> None:
    cfg = FactorSleeveConfig(
        enabled=True,
        long_top_n=1,
        short_bottom_n=1,
        neutralise_by_asset_class=False,
    )
    sleeve = FactorSleeve(cfg)
    rows = [
        FactorUniverseInput(
            symbol=f"S{i}",
            asset_class="equity",
            close=_make_series(drift=d, seed=i),
        )
        for i, d in enumerate([0.003, 0.0, -0.003])
    ]
    cands, _ = sleeve.evaluate(rows)
    sides = sorted({c.side for c in cands})
    assert sides == ["long", "short"]


# ── 7. config from dict ─────────────────────────────────────────────────────


def test_factor_sleeve_config_from_dict_overrides_blend() -> None:
    raw = {
        "factor_sleeve": {
            "enabled": True,
            "long_top_n": 5,
            "blend": {
                "families": [
                    {
                        "name": "only_momentum",
                        "weight": 1.0,
                        "members": [{"name": "momentum_12_1", "weight": 1.0}],
                    }
                ]
            },
        }
    }
    cfg = FactorSleeveConfig.from_dict(raw)
    assert cfg.enabled is True
    assert cfg.long_top_n == 5
    assert len(cfg.blend.families) == 1
    assert cfg.blend.families[0].name == "only_momentum"
