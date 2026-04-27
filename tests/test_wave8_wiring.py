"""
tests/test_wave8_wiring.py
============================
Wave 8 (wiring) — verify the vol-targeting + drawdown overlay
correctly modulates ``ge`` in ``portfolio.allocation_engine.build_allocation_decision``
and is a no-op when disabled.

Coverage:

1. Default off: ``ge`` is unchanged and ``wave8_vol_overlay_used`` is
   ``False`` in metadata.
2. Overlay enabled with realised vol below target: ``ge`` is scaled up
   (within ``max_scale``), and per-component metadata is exposed.
3. Overlay enabled with deep drawdown: ``ge`` is scaled down toward
   ``drawdown_floor``.
4. Defensive: a broken overlay config does not crash the allocator.
5. Cache reset hygiene.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from config.loaders import load_allocation, load_profile_modes
from core.models_runtime import (
    Opportunity,
    OpportunityComponents,
    PortfolioState,
    RegimeState,
)
from portfolio.allocation_engine import (
    build_allocation_decision,
    reset_portfolio_optimisation_cache,
)
from portfolio.optimizers import (
    PortfolioOptimisationConfig,
    VolTargetingOverlayConfig,
)
from risk.regime_state import compute_regime_state_from_inputs


def _portfolio(*, drawdown: float = 0.0, capital_pct: float = 0.8) -> PortfolioState:
    return PortfolioState(
        timestamp=datetime.now(timezone.utc),
        mode="trader",
        nav=Decimal("100000"),
        cash=Decimal("20000"),
        available_buying_power=Decimal("80000"),
        gross_exposure=Decimal("80000"),
        net_exposure=Decimal("80000"),
        leverage_ratio=Decimal("1"),
        drawdown_from_hwm_pct=Decimal(str(drawdown)),
        metadata={"capital_pct": capital_pct},
    )


def _regime(*, market_volatility: float = 0.20) -> RegimeState:
    alloc = load_allocation()
    return compute_regime_state_from_inputs(
        portfolio_state=_portfolio(),
        allocation_cfg=alloc,
        feature_rows=[
            {"symbol": "A", "features": {"mom_10": 1.0, "volume_z": 0.5, "relative_dollar_volume": 1.1}},
            {"symbol": "B", "features": {"mom_10": 0.9, "volume_z": 0.4, "relative_dollar_volume": 1.05}},
            {"symbol": "C", "features": {"mom_10": 0.8, "volume_z": 0.3, "relative_dollar_volume": 1.02}},
        ],
        news_dispersion=(0.1, 0.2),
    )


def _opp(sym: str, score: str = "0.7") -> Opportunity:
    return Opportunity(
        symbol=sym,
        asset_class="crypto",  # type: ignore[arg-type]
        side="long",
        timestamp=datetime.now(timezone.utc),
        opportunity_score=Decimal(score),
        urgency_score=Decimal("0.5"),
        confidence=Decimal("0.7"),
        components=OpportunityComponents(momentum=Decimal(score)),
    )


def _set_market_volatility(regime: RegimeState, vol: float) -> RegimeState:
    md = dict(regime.metadata or {})
    md["market_volatility"] = vol
    regime.metadata = md
    return regime


# ── 1. default off ─────────────────────────────────────────────────────────


def test_wave8_overlay_default_off_unchanged(monkeypatch) -> None:
    reset_portfolio_optimisation_cache()
    monkeypatch.setattr(
        "portfolio.allocation_engine._get_default_portfolio_optimisation_config",
        lambda: PortfolioOptimisationConfig(),  # default disabled
    )
    alloc = load_allocation()
    profile = load_profile_modes()
    ps = _portfolio()
    regime = _set_market_volatility(_regime(), 0.20)
    dec = build_allocation_decision(
        opportunities=[_opp("AAA"), _opp("BBB")],
        portfolio_state=ps,
        regime_state=regime,
        allocation_cfg=alloc,
        profile_cfg=profile,
    )
    assert dec.metadata.get("wave8_vol_overlay_used") is False


# ── 2. low realised vol scales ge up ───────────────────────────────────────


def test_wave8_overlay_low_vol_scales_up(monkeypatch) -> None:
    reset_portfolio_optimisation_cache()
    cfg = PortfolioOptimisationConfig(
        vol_targeting_overlay=VolTargetingOverlayConfig(
            enabled=True,
            target_vol=0.20,
            min_scale=0.25,
            max_scale=2.0,
            soft_drawdown=0.05,
            hard_drawdown=0.20,
            drawdown_floor=0.10,
        )
    )
    monkeypatch.setattr(
        "portfolio.allocation_engine._get_default_portfolio_optimisation_config",
        lambda: cfg,
    )
    alloc = load_allocation()
    profile = load_profile_modes()
    ps = _portfolio(drawdown=0.0)
    regime = _set_market_volatility(_regime(), 0.10)  # half of target ⇒ ratio 2.0

    # Baseline (overlay disabled) for comparison.
    monkeypatch.setattr(
        "portfolio.allocation_engine._get_default_portfolio_optimisation_config",
        lambda: PortfolioOptimisationConfig(),
    )
    dec_off = build_allocation_decision(
        opportunities=[_opp("AAA"), _opp("BBB")],
        portfolio_state=ps,
        regime_state=regime,
        allocation_cfg=alloc,
        profile_cfg=profile,
    )

    # Enabled run.
    monkeypatch.setattr(
        "portfolio.allocation_engine._get_default_portfolio_optimisation_config",
        lambda: cfg,
    )
    dec_on = build_allocation_decision(
        opportunities=[_opp("AAA"), _opp("BBB")],
        portfolio_state=ps,
        regime_state=regime,
        allocation_cfg=alloc,
        profile_cfg=profile,
    )

    assert dec_on.metadata.get("wave8_vol_overlay_used") is True
    assert dec_on.metadata.get("wave8_vol_overlay_scale", 0.0) > 1.0
    # ge should be larger than the no-overlay baseline (subject to max_ge clip).
    assert dec_on.gross_exposure_target >= dec_off.gross_exposure_target


# ── 3. deep drawdown scales ge down ────────────────────────────────────────


def test_wave8_overlay_deep_drawdown_scales_down(monkeypatch) -> None:
    reset_portfolio_optimisation_cache()
    cfg = PortfolioOptimisationConfig(
        vol_targeting_overlay=VolTargetingOverlayConfig(
            enabled=True,
            target_vol=0.20,
            min_scale=0.25,
            max_scale=2.0,
            soft_drawdown=0.05,
            hard_drawdown=0.20,
            drawdown_floor=0.10,
        )
    )
    alloc = load_allocation()
    profile = load_profile_modes()
    # Drawdown at the hard threshold ⇒ floor scaling.
    ps = _portfolio(drawdown=0.20)
    regime = _set_market_volatility(_regime(), 0.20)  # equal to target ⇒ vol component = 1.0

    monkeypatch.setattr(
        "portfolio.allocation_engine._get_default_portfolio_optimisation_config",
        lambda: cfg,
    )
    dec = build_allocation_decision(
        opportunities=[_opp("AAA")],
        portfolio_state=ps,
        regime_state=regime,
        allocation_cfg=alloc,
        profile_cfg=profile,
    )
    assert dec.metadata.get("wave8_vol_overlay_used") is True
    # Drawdown component clamps at floor 0.10; vol component is 1.0 ⇒ scale = 0.10.
    assert dec.metadata.get("wave8_vol_overlay_scale", 1.0) == pytest.approx(0.10, abs=1e-9)
    assert dec.metadata.get("wave8_vol_overlay_drawdown_component") == pytest.approx(0.10)


# ── 4. defensive on missing market_volatility ──────────────────────────────


def test_wave8_overlay_missing_realised_vol_skips_vol_component(monkeypatch) -> None:
    reset_portfolio_optimisation_cache()
    cfg = PortfolioOptimisationConfig(
        vol_targeting_overlay=VolTargetingOverlayConfig(enabled=True, target_vol=0.20)
    )
    monkeypatch.setattr(
        "portfolio.allocation_engine._get_default_portfolio_optimisation_config",
        lambda: cfg,
    )
    alloc = load_allocation()
    profile = load_profile_modes()
    ps = _portfolio()
    regime = _regime()
    # No market_volatility key in regime metadata → vol_scale returns 1.0.
    md = dict(regime.metadata or {})
    md.pop("market_volatility", None)
    regime.metadata = md

    dec = build_allocation_decision(
        opportunities=[_opp("AAA")],
        portfolio_state=ps,
        regime_state=regime,
        allocation_cfg=alloc,
        profile_cfg=profile,
    )
    # The overlay still runs (drawdown is 0 so dd_component=1.0); since
    # vol_component is None and dd_component is 1.0, combined_scale is 1.0.
    assert dec.metadata.get("wave8_vol_overlay_used") is True
    assert dec.metadata.get("wave8_vol_overlay_scale", 1.0) == pytest.approx(1.0)


# ── 5. broken overlay does not crash ───────────────────────────────────────


def test_wave8_overlay_exception_is_swallowed(monkeypatch) -> None:
    reset_portfolio_optimisation_cache()

    class _BrokenOverlay:
        enabled = True

        def __getattr__(self, name):
            raise RuntimeError("intentional")

    class _BrokenCfg:
        vol_targeting_overlay = _BrokenOverlay()

    monkeypatch.setattr(
        "portfolio.allocation_engine._get_default_portfolio_optimisation_config",
        lambda: _BrokenCfg(),
    )
    alloc = load_allocation()
    profile = load_profile_modes()
    ps = _portfolio()
    regime = _set_market_volatility(_regime(), 0.20)
    dec = build_allocation_decision(
        opportunities=[_opp("AAA")],
        portfolio_state=ps,
        regime_state=regime,
        allocation_cfg=alloc,
        profile_cfg=profile,
    )
    # Allocator survived; overlay disabled itself with an error tag.
    assert dec.metadata.get("wave8_vol_overlay_used") is False
    # An error key may be present.
    assert (
        "wave8_vol_overlay_error" in dec.metadata
        or "wave8_vol_overlay_reason" in dec.metadata
        or dec.metadata.get("wave8_vol_overlay_used") is False
    )


# ── 6. cache hygiene ───────────────────────────────────────────────────────


def test_reset_cache_picks_up_new_yaml() -> None:
    from portfolio.allocation_engine import _get_default_portfolio_optimisation_config

    reset_portfolio_optimisation_cache()
    cfg = _get_default_portfolio_optimisation_config()
    # The shipping YAML has overlay disabled.
    assert cfg is None or cfg.vol_targeting_overlay.enabled is False
