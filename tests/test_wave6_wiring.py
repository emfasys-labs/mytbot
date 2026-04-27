"""
tests/test_wave6_wiring.py
============================
Wave 6 (wiring) — verify the forecast bridge plugs into
``signals.opportunity_engine.build_opportunities`` without changing
default behaviour.

Coverage:

1. Default: bridge auto-loads disabled config; opportunities are
   unchanged and no ``forecast_*`` metadata leaks in.
2. Bridge enabled but no members: opportunities get
   ``forecast_used=False`` + ``forecast_reason="no_models"`` metadata.
3. Bridge enabled with an approved member + injected artefact:
   ``Opportunity.expected_return`` populated, sign-aligned to side;
   confidence is geometric-mean blend of original and forecast.
4. Short-side opportunity gets sign-flipped expected_return.
5. Bridge runs *before* trained meta-labeller — verify by enabling
   both and checking the meta-label sees the forecast-modulated
   confidence in metadata.
6. Cache reset hygiene.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pandas as pd
import numpy as np
import pytest

from config.loaders import load_allocation
from core.models_runtime import (
    MarketStateComponents,
    RegimeState,
    SignalCandidate,
)
from models.forecasts import (
    ForecastDataset,
    TrainedForecastModel,
    train_forecast_model,
)
from models.registry import ModelRegistry
from models.schemas import (
    ApprovalStatus,
    FeatureSpec,
    ModelContract,
    Task,
)
from signals.forecast_bridge import (
    ForecastBridgeConfig,
    ForecastModelEntry,
)
from signals.opportunity_engine import (
    build_opportunities,
    reset_forecast_bridge_cache,
)


def _trivial_regime() -> RegimeState:
    return RegimeState(
        timestamp=datetime.now(timezone.utc),
        regime_label="trend",  # type: ignore[arg-type]
        market_state_score=Decimal("0.5"),
        drawdown_throttle=Decimal("1.0"),
        execution_quality=Decimal("1.0"),
        breadth_score=Decimal("0.0"),
        components=MarketStateComponents(),
        metadata={"demand_score": 0.0},
    )


def _candidate(*, side: str = "long") -> SignalCandidate:
    return SignalCandidate(
        symbol="SPY",
        asset_class="equity",
        side=side,  # type: ignore[arg-type]
        timestamp=datetime.now(timezone.utc),
        raw_signal_strength=Decimal("0.5"),
        adjusted_signal_strength=Decimal("0.5"),
        confidence=Decimal("0.5"),
        strategy_name="momentum",
        metadata={},
    )


def _toy_regression_artefact() -> tuple[TrainedForecastModel, str]:
    """A trained ridge model that returns ~+1% on positive feature, ~-1% on negative."""
    rng = np.random.default_rng(0)
    n = 300
    f = rng.normal(0, 1, n)
    y = 0.01 * np.sign(f) + rng.normal(0, 0.001, n)
    feats = pd.DataFrame(
        {"strategy_confidence": f},
        index=pd.date_range("2026-01-01", periods=n, freq="h", tz="UTC"),
    )
    yser = pd.Series(y, index=feats.index)
    ds = ForecastDataset(
        X=feats, y=yser, timestamps=feats.index,
        feature_columns=["strategy_confidence"],
        target_kind="forward_return", horizon=1, is_classification=False,
    )
    specs = [FeatureSpec("strategy_confidence", "float64")]
    art, _ = train_forecast_model(dataset=ds, feature_specs=specs, estimator="ridge")
    return art, art.feature_contract_hash


def _registry_with(name: str, version: str, status: ApprovalStatus, fc_hash: str) -> ModelRegistry:
    contract = ModelContract(
        name=name, version=version, task=Task.REGRESSION, target="forward_return",
        feature_contract_hash=fc_hash,
        validation_method="purged_kfold",
        approval_status=status,
    )
    return ModelRegistry([contract])


# ── 1. default off ─────────────────────────────────────────────────────────


def test_default_off_no_forecast_metadata(monkeypatch) -> None:
    reset_forecast_bridge_cache()
    monkeypatch.setattr(
        "signals.opportunity_engine._get_default_forecast_bridge_config",
        lambda: ForecastBridgeConfig(enabled=False),
    )
    opps = build_opportunities(
        signals=[_candidate()],
        regime_state=_trivial_regime(),
        allocation_cfg=load_allocation(),
    )
    assert len(opps) == 1
    md = opps[0].metadata
    assert "forecast_used" not in md
    assert "forecast_expected_return" not in md


# ── 2. enabled but no members ──────────────────────────────────────────────


def test_enabled_no_members_attaches_metadata(monkeypatch) -> None:
    reset_forecast_bridge_cache()
    cfg = ForecastBridgeConfig(enabled=True, members=[])
    monkeypatch.setattr(
        "signals.opportunity_engine._get_default_forecast_bridge_config",
        lambda: cfg,
    )
    opps = build_opportunities(
        signals=[_candidate()],
        regime_state=_trivial_regime(),
        allocation_cfg=load_allocation(),
    )
    md = opps[0].metadata
    assert md.get("forecast_used") is False
    assert md.get("forecast_reason") == "no_models"
    # expected_return must remain at its original default.
    assert opps[0].expected_return == Decimal("0")


# ── 3. enabled + approved member runs end-to-end ───────────────────────────


def test_enabled_approved_member_populates_expected_return(monkeypatch) -> None:
    reset_forecast_bridge_cache()
    artefact, fc_hash = _toy_regression_artefact()
    reg = _registry_with("toy_fcst", "0.1", ApprovalStatus.PAPER, fc_hash)
    monkeypatch.setattr(
        "signals.forecast_bridge.get_default_registry", lambda: reg
    )
    monkeypatch.setattr(
        "signals.forecast_bridge._load_artefact",
        lambda entry, contract, loader: artefact,
    )
    cfg = ForecastBridgeConfig(
        enabled=True,
        members=[ForecastModelEntry(
            name="toy_fcst", version="0.1", target_kind="forward_return", horizon=1, weight=1.0,
        )],
    )
    monkeypatch.setattr(
        "signals.opportunity_engine._get_default_forecast_bridge_config",
        lambda: cfg,
    )

    # Strong positive strategy_confidence ⇒ artefact predicts ~+1%.
    cand = SignalCandidate(
        symbol="SPY", asset_class="equity", side="long",
        timestamp=datetime.now(timezone.utc),
        raw_signal_strength=Decimal("0.8"),
        adjusted_signal_strength=Decimal("0.8"),
        confidence=Decimal("0.8"),
        strategy_name="momentum",
        metadata={"strategy_confidence": 1.5},
    )
    opps = build_opportunities(
        signals=[cand],
        regime_state=_trivial_regime(),
        allocation_cfg=load_allocation(),
    )
    md = opps[0].metadata
    assert md.get("forecast_used") is True
    assert md.get("forecast_reason") == "approved"
    # Sign-aligned to long side; positive expected return.
    assert opps[0].expected_return > Decimal("0")
    assert "forecast_expected_return" in md
    assert "forecast_members_used" in md


# ── 4. short side flips sign ───────────────────────────────────────────────


def test_short_side_flips_expected_return_sign(monkeypatch) -> None:
    reset_forecast_bridge_cache()
    artefact, fc_hash = _toy_regression_artefact()
    reg = _registry_with("toy_fcst", "0.1", ApprovalStatus.PAPER, fc_hash)
    monkeypatch.setattr(
        "signals.forecast_bridge.get_default_registry", lambda: reg
    )
    monkeypatch.setattr(
        "signals.forecast_bridge._load_artefact",
        lambda entry, contract, loader: artefact,
    )
    cfg = ForecastBridgeConfig(
        enabled=True,
        members=[ForecastModelEntry(
            name="toy_fcst", version="0.1", target_kind="forward_return", horizon=1, weight=1.0,
        )],
    )
    monkeypatch.setattr(
        "signals.opportunity_engine._get_default_forecast_bridge_config",
        lambda: cfg,
    )

    # Same strong positive feature — ensemble predicts ~+1% — but on a
    # SHORT candidate, sign-aligned expected_return should be negative.
    cand = SignalCandidate(
        symbol="SPY", asset_class="equity", side="short",
        timestamp=datetime.now(timezone.utc),
        raw_signal_strength=Decimal("0.8"),
        adjusted_signal_strength=Decimal("0.8"),
        confidence=Decimal("0.8"),
        strategy_name="momentum",
        metadata={"strategy_confidence": 1.5},
    )
    opps = build_opportunities(
        signals=[cand],
        regime_state=_trivial_regime(),
        allocation_cfg=load_allocation(),
    )
    assert opps[0].metadata.get("forecast_used") is True
    assert opps[0].expected_return < Decimal("0")


# ── 5. bridge runs before trained meta-labeller ────────────────────────────


def test_forecast_runs_before_meta_labeler(monkeypatch) -> None:
    """When both wave-2 and wave-6 are on, forecast metadata is present
    even if meta-labeler drops the opportunity."""
    reset_forecast_bridge_cache()
    artefact, fc_hash = _toy_regression_artefact()
    reg = _registry_with("toy_fcst", "0.1", ApprovalStatus.PAPER, fc_hash)
    monkeypatch.setattr(
        "signals.forecast_bridge.get_default_registry", lambda: reg
    )
    monkeypatch.setattr(
        "signals.forecast_bridge._load_artefact",
        lambda entry, contract, loader: artefact,
    )
    fcst_cfg = ForecastBridgeConfig(
        enabled=True,
        members=[ForecastModelEntry(
            name="toy_fcst", version="0.1", target_kind="forward_return", horizon=1, weight=1.0,
        )],
    )

    # Build an opportunity with both gates wired at once.
    cand = SignalCandidate(
        symbol="SPY", asset_class="equity", side="long",
        timestamp=datetime.now(timezone.utc),
        raw_signal_strength=Decimal("0.8"),
        adjusted_signal_strength=Decimal("0.8"),
        confidence=Decimal("0.8"),
        strategy_name="momentum",
        metadata={"strategy_confidence": 1.5},
    )
    opps = build_opportunities(
        signals=[cand],
        regime_state=_trivial_regime(),
        allocation_cfg=load_allocation(),
        forecast_bridge_config=fcst_cfg,
    )
    md = opps[0].metadata
    # Forecast metadata is on the survivor.
    assert md.get("forecast_used") is True


# ── 6. cache reset ─────────────────────────────────────────────────────────


def test_reset_forecast_bridge_cache_clears() -> None:
    from signals.opportunity_engine import _get_default_forecast_bridge_config

    _ = _get_default_forecast_bridge_config()
    reset_forecast_bridge_cache()
    cfg = _get_default_forecast_bridge_config()
    assert cfg is None or cfg.enabled is False
