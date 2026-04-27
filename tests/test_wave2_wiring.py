"""
tests/test_wave2_wiring.py
============================
Wave 2 (wiring) — verify that ``SignalEngine`` and
``opportunity_engine.build_opportunities`` honour the trained
meta-labeller flag without changing baseline behaviour.

Critical guarantees:

1. With ``use_trained_meta_labeler`` absent / False (the default),
   ``SignalEngine.process`` and ``SignalEngine.raw_to_signal_candidate``
   produce identical output to the pre-Wave-2 engine. No metadata is
   added, no signal is dropped.
2. With the flag True but no model registered, both call sites
   pass-through and tag ``meta_label_reason``.
3. With a registered+approved (paper) model and an injected artefact,
   the engine drops candidates whose probability is below the
   threshold, attaches metadata, and forwards the rest unchanged.
4. ``build_opportunities`` honours the same gate via its
   ``trained_meta_labeler_config`` kwarg (default: no behaviour change).
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import numpy as np
import pandas as pd
import pytest

from config.loaders import load_allocation
from core.models_runtime import (
    MarketStateComponents,
    Opportunity,
    RegimeState,
    SignalCandidate,
)
from models.meta_label import (
    ThresholdConfig,
    train_meta_label_model,
)
from models.registry import ModelRegistry
from models.schemas import (
    ApprovalStatus,
    FeatureSpec,
    ModelContract,
    Task,
)
from signals.engine import RawSignal, SignalEngine
from signals.opportunity_engine import build_opportunities
from signals.trained_meta_labeler import TrainedMetaLabelerConfig


# ── helpers ─────────────────────────────────────────────────────────────────


def _basic_raw(side: str = "buy", confidence: float = 0.7) -> RawSignal:
    return RawSignal(
        strategy="momentum",
        symbol="SPY",
        side=side,
        confidence=confidence,
        broker="ibkr",
        asset_class="equity",
        metadata={"close": 100.0},
    )


def _train_toy_artefact():
    feats = [FeatureSpec("strategy_confidence", "float64"), FeatureSpec("side_sign", "float64")]
    rng = np.random.default_rng(0)
    X = pd.DataFrame(
        {
            "strategy_confidence": rng.uniform(0, 1, 200),
            "side_sign": rng.choice([-1.0, 1.0], 200),
        }
    )
    # Higher confidence + buy side ⇒ label 1; otherwise 0. Deterministic.
    y = ((X["strategy_confidence"] > 0.5) & (X["side_sign"] > 0)).astype(int)
    artefact, _ = train_meta_label_model(
        X=X, y=y, feature_specs=feats, classifier="logreg", calibration="platt"
    )
    return artefact, feats


def _registry_with(name: str, version: str, status: ApprovalStatus, fc_hash: str) -> ModelRegistry:
    contract = ModelContract(
        name=name,
        version=version,
        task=Task.CLASSIFICATION,
        target="triple_barrier_outcome",
        feature_contract_hash=fc_hash,
        validation_method="purged_kfold",
        approval_status=status,
    )
    return ModelRegistry([contract])


# ── 1. default-off: zero behaviour change ──────────────────────────────────


def test_signal_engine_default_off_baseline_unchanged():
    engine = SignalEngine({"default_position_pct": 0.1, "quantity_decimals": 6})
    sig = engine.process(_basic_raw(), portfolio_value=Decimal("10000"))
    assert sig is not None
    assert "meta_label_reason" not in sig.metadata
    assert "meta_label_probability" not in sig.metadata


def test_raw_to_signal_candidate_default_off_baseline_unchanged():
    engine = SignalEngine({})
    cand = engine.raw_to_signal_candidate(_basic_raw())
    assert cand is not None
    assert "meta_label_reason" not in cand.metadata


# ── 2. flag on, no registered model: pass-through ──────────────────────────


def test_signal_engine_flag_on_no_model_passthrough(monkeypatch):
    # Replace the loader so the SignalEngine picks up an enabled-but-no-model
    # config without touching the on-disk YAML.
    cfg = TrainedMetaLabelerConfig(enabled=True, model_name=None)
    monkeypatch.setattr(
        "signals.trained_meta_labeler.TrainedMetaLabelerConfig.load",
        classmethod(lambda cls, path=None: cfg),
    )

    engine = SignalEngine({"use_trained_meta_labeler": True})
    sig = engine.process(_basic_raw(), portfolio_value=Decimal("10000"))
    assert sig is not None
    assert sig.metadata.get("meta_label_reason") == "no_model_passthrough"
    assert sig.metadata.get("meta_label_kept") is True


# ── 3. flag on, model registered + injected artefact: gating works ─────────


def test_signal_engine_drops_signal_below_threshold(monkeypatch):
    artefact, _ = _train_toy_artefact()
    fc_hash = artefact.feature_contract_hash

    cfg = TrainedMetaLabelerConfig(
        enabled=True,
        model_name="toy",
        model_version="0.1",
        thresholds=ThresholdConfig(default=0.95),
    )
    monkeypatch.setattr(
        "signals.trained_meta_labeler.TrainedMetaLabelerConfig.load",
        classmethod(lambda cls, path=None: cfg),
    )
    reg = _registry_with("toy", "0.1", ApprovalStatus.PAPER, fc_hash)
    monkeypatch.setattr(
        "signals.trained_meta_labeler.get_default_registry", lambda: reg
    )
    monkeypatch.setattr(
        "signals.trained_meta_labeler._load_artefact",
        lambda cfg, contract, loader: artefact,
    )

    engine = SignalEngine({"use_trained_meta_labeler": True})
    sig = engine.process(_basic_raw(side="sell"), portfolio_value=Decimal("10000"))
    assert sig is None


def test_signal_engine_keeps_signal_when_above_threshold(monkeypatch):
    artefact, _ = _train_toy_artefact()
    fc_hash = artefact.feature_contract_hash

    cfg = TrainedMetaLabelerConfig(
        enabled=True,
        model_name="toy",
        model_version="0.1",
        thresholds=ThresholdConfig(default=0.05),
    )
    monkeypatch.setattr(
        "signals.trained_meta_labeler.TrainedMetaLabelerConfig.load",
        classmethod(lambda cls, path=None: cfg),
    )
    reg = _registry_with("toy", "0.1", ApprovalStatus.PAPER, fc_hash)
    monkeypatch.setattr(
        "signals.trained_meta_labeler.get_default_registry", lambda: reg
    )
    monkeypatch.setattr(
        "signals.trained_meta_labeler._load_artefact",
        lambda cfg, contract, loader: artefact,
    )

    engine = SignalEngine({"use_trained_meta_labeler": True})
    sig = engine.process(_basic_raw(side="buy", confidence=0.9), portfolio_value=Decimal("10000"))
    assert sig is not None
    assert sig.metadata.get("meta_label_kept") is True
    assert sig.metadata.get("meta_label_reason") == "approved"
    assert isinstance(sig.metadata.get("meta_label_probability"), float)
    assert sig.metadata.get("meta_label_model_name") == "toy"


# ── 4. opportunity engine wiring ───────────────────────────────────────────


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


def _candidate(
    *, side: str = "long", confidence: Decimal = Decimal("0.7")
) -> SignalCandidate:
    return SignalCandidate(
        symbol="SPY",
        asset_class="equity",
        side=side,  # type: ignore[arg-type]
        timestamp=datetime.now(timezone.utc),
        raw_signal_strength=confidence,
        adjusted_signal_strength=confidence,
        confidence=confidence,
        strategy_name="momentum",
        metadata={},
    )


def test_build_opportunities_default_off_unchanged():
    cfg = load_allocation()
    opps = build_opportunities(
        signals=[_candidate()],
        regime_state=_trivial_regime(),
        allocation_cfg=cfg,
    )
    assert len(opps) == 1
    assert "meta_label_reason" not in opps[0].metadata


def test_build_opportunities_drops_when_meta_label_below_threshold(monkeypatch):
    artefact, _ = _train_toy_artefact()
    fc_hash = artefact.feature_contract_hash
    reg = _registry_with("toy", "0.1", ApprovalStatus.PAPER, fc_hash)
    monkeypatch.setattr(
        "signals.trained_meta_labeler.get_default_registry", lambda: reg
    )
    monkeypatch.setattr(
        "signals.trained_meta_labeler._load_artefact",
        lambda cfg, contract, loader: artefact,
    )

    cfg = load_allocation()
    tml_cfg = TrainedMetaLabelerConfig(
        enabled=True,
        model_name="toy",
        model_version="0.1",
        thresholds=ThresholdConfig(default=0.95),  # impossible
    )
    opps = build_opportunities(
        signals=[_candidate(side="short")],
        regime_state=_trivial_regime(),
        allocation_cfg=cfg,
        trained_meta_labeler_config=tml_cfg,
    )
    assert opps == []


def test_build_opportunities_attaches_metadata_when_kept(monkeypatch):
    artefact, _ = _train_toy_artefact()
    fc_hash = artefact.feature_contract_hash
    reg = _registry_with("toy", "0.1", ApprovalStatus.PAPER, fc_hash)
    monkeypatch.setattr(
        "signals.trained_meta_labeler.get_default_registry", lambda: reg
    )
    monkeypatch.setattr(
        "signals.trained_meta_labeler._load_artefact",
        lambda cfg, contract, loader: artefact,
    )

    cfg = load_allocation()
    tml_cfg = TrainedMetaLabelerConfig(
        enabled=True,
        model_name="toy",
        model_version="0.1",
        thresholds=ThresholdConfig(default=0.05),
    )
    opps = build_opportunities(
        signals=[_candidate(side="long", confidence=Decimal("0.9"))],
        regime_state=_trivial_regime(),
        allocation_cfg=cfg,
        trained_meta_labeler_config=tml_cfg,
    )
    assert len(opps) == 1
    md = opps[0].metadata
    assert md.get("meta_label_reason") == "approved"
    assert md.get("meta_label_model_name") == "toy"
    assert isinstance(md.get("meta_label_probability"), float)


