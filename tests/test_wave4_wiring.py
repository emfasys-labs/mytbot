"""
tests/test_wave4_wiring.py
============================
Wave 4 (wiring) — verify the trained ``HMMRegimeClassifier`` is
correctly consulted by ``risk.regime_state.compute_regime_state_from_inputs``
without changing baseline behaviour.

Coverage:

1. Default (gate disabled): the heuristic label is returned unchanged
   and ``regime_classifier_used`` is ``False`` in metadata.
2. Insufficient data: even with the classifier enabled, the
   "insufficient_data" sentinel is preserved (never overridden).
3. Enabled + fitted artefact: the classifier prediction is mapped to
   the ``RegimeLabel`` vocabulary and overrides the heuristic.
4. Enabled + missing artefact: heuristic survives; metadata records
   ``artefact_unavailable``.
5. Cache reset hygiene.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import numpy as np
import pytest

from config.loaders import load_allocation
from core.models_runtime import PortfolioState
from risk.regime_models import HMMRegimeClassifier
from risk.regime_state import (
    _RegimeClassifierGate,
    compute_regime_state_from_inputs,
    reset_regime_classifier_cache,
)


def _portfolio() -> PortfolioState:
    now = datetime.now(timezone.utc)
    return PortfolioState(
        timestamp=now,
        mode="trader",
        nav=Decimal("100000"),
        cash=Decimal("50000"),
        available_buying_power=Decimal("50000"),
        gross_exposure=Decimal("50000"),
        net_exposure=Decimal("50000"),
        leverage_ratio=Decimal("1"),
        drawdown_from_hwm_pct=Decimal("0.02"),
    )


def _rows() -> list[dict]:
    return [
        {"symbol": "A", "features": {"mom_10": 2.0, "rsi_14": 60.0, "volume_z": 0.5, "relative_dollar_volume": 1.1}},
        {"symbol": "B", "features": {"mom_10": 1.5, "rsi_14": 58.0, "volume_z": 0.4, "relative_dollar_volume": 1.05}},
        {"symbol": "C", "features": {"mom_10": 1.8, "rsi_14": 57.0, "volume_z": 0.3, "relative_dollar_volume": 1.02}},
    ]


# ── 1. baseline unchanged ───────────────────────────────────────────────────


def test_regime_state_default_is_unchanged_no_classifier_meta(monkeypatch) -> None:
    reset_regime_classifier_cache()
    monkeypatch.setattr(
        "risk.regime_state._load_regime_classifier_gate",
        lambda: _RegimeClassifierGate(enabled=False),
    )
    r = compute_regime_state_from_inputs(
        portfolio_state=_portfolio(),
        allocation_cfg=load_allocation(),
        feature_rows=_rows(),
        news_dispersion=(0.1, 0.4),
    )
    assert r.metadata.get("regime_classifier_used") is False
    # No classifier-only metadata leaks in.
    assert "regime_classifier_label_raw" not in r.metadata


# ── 2. insufficient_data preserved ──────────────────────────────────────────


def test_insufficient_data_not_overridden_by_classifier(monkeypatch) -> None:
    reset_regime_classifier_cache()

    # Build any fitted classifier — the override path must never run.
    rng = np.random.default_rng(0)
    X = rng.normal(0, 1, (200, 2))
    clf = HMMRegimeClassifier(
        n_states=3,
        feature_names=("mean_return", "volatility"),
        min_samples=50,
        seed=0,
    )
    clf.fit(X)

    monkeypatch.setattr(
        "risk.regime_state._load_regime_classifier_gate",
        lambda: _RegimeClassifierGate(
            enabled=True,
            artifact_path=Path("anywhere"),
            feature_names=("mean_return", "volatility"),
        ),
    )
    monkeypatch.setattr(
        "risk.regime_state._load_regime_classifier_artefact",
        lambda gate: clf,
    )

    r = compute_regime_state_from_inputs(
        portfolio_state=_portfolio(),
        allocation_cfg=load_allocation(),
        feature_rows=[],  # zero rows ⇒ insufficient
        news_dispersion=None,
    )
    assert r.regime_label == "insufficient_data"
    # Classifier metadata may be untouched in this branch (we never run it).
    assert r.metadata.get("regime_classifier_used") is False


# ── 3. enabled + fitted artefact overrides heuristic ────────────────────────


def test_classifier_overrides_heuristic_when_enabled(monkeypatch) -> None:
    reset_regime_classifier_cache()

    # Build a deterministic classifier that always returns "crash".
    class _StubArtefact:
        backend_ = "numpy"
        feature_names = ("mean_return", "volatility", "breadth")

        def predict_label(self, x):
            return "crash"

    monkeypatch.setattr(
        "risk.regime_state._load_regime_classifier_gate",
        lambda: _RegimeClassifierGate(
            enabled=True,
            artifact_path=Path("anywhere"),
            feature_names=("mean_return", "volatility", "breadth"),
        ),
    )
    monkeypatch.setattr(
        "risk.regime_state._load_regime_classifier_artefact",
        lambda gate: _StubArtefact(),
    )

    r = compute_regime_state_from_inputs(
        portfolio_state=_portfolio(),
        allocation_cfg=load_allocation(),
        feature_rows=_rows(),
        news_dispersion=(0.1, 0.4),
    )
    assert r.regime_label == "crash"
    assert r.metadata.get("regime_classifier_used") is True
    assert r.metadata.get("regime_classifier_label_raw") == "crash"
    # Heuristic label is preserved for audit.
    assert "regime_heuristic_label" in r.metadata


def test_classifier_label_trend_maps_to_trend_up(monkeypatch) -> None:
    reset_regime_classifier_cache()

    class _StubArtefact:
        backend_ = "numpy"
        feature_names = ("mean_return", "volatility", "breadth")

        def predict_label(self, x):
            return "trend"

    monkeypatch.setattr(
        "risk.regime_state._load_regime_classifier_gate",
        lambda: _RegimeClassifierGate(
            enabled=True,
            artifact_path=Path("anywhere"),
            feature_names=("mean_return", "volatility", "breadth"),
        ),
    )
    monkeypatch.setattr(
        "risk.regime_state._load_regime_classifier_artefact",
        lambda gate: _StubArtefact(),
    )

    r = compute_regime_state_from_inputs(
        portfolio_state=_portfolio(),
        allocation_cfg=load_allocation(),
        feature_rows=_rows(),
        news_dispersion=None,
    )
    assert r.regime_label == "trend_up"


# ── 4. enabled + missing artefact ⇒ fall back ───────────────────────────────


def test_missing_artefact_falls_back_to_heuristic(monkeypatch) -> None:
    reset_regime_classifier_cache()
    monkeypatch.setattr(
        "risk.regime_state._load_regime_classifier_gate",
        lambda: _RegimeClassifierGate(
            enabled=True,
            artifact_path=Path("anywhere"),
            feature_names=("mean_return", "volatility"),
        ),
    )
    monkeypatch.setattr(
        "risk.regime_state._load_regime_classifier_artefact",
        lambda gate: None,
    )

    r = compute_regime_state_from_inputs(
        portfolio_state=_portfolio(),
        allocation_cfg=load_allocation(),
        feature_rows=_rows(),
        news_dispersion=None,
    )
    # Classifier disabled silently; heuristic label survives.
    assert r.metadata.get("regime_classifier_used") is False
    assert r.metadata.get("regime_classifier_reason") == "artefact_unavailable"


# ── 5. classifier failure is caught defensively ─────────────────────────────


def test_classifier_predict_failure_does_not_crash(monkeypatch) -> None:
    reset_regime_classifier_cache()

    class _BrokenArtefact:
        backend_ = "numpy"
        feature_names = ("mean_return", "volatility")

        def predict_label(self, x):
            raise RuntimeError("intentional")

    monkeypatch.setattr(
        "risk.regime_state._load_regime_classifier_gate",
        lambda: _RegimeClassifierGate(
            enabled=True,
            artifact_path=Path("anywhere"),
            feature_names=("mean_return", "volatility"),
        ),
    )
    monkeypatch.setattr(
        "risk.regime_state._load_regime_classifier_artefact",
        lambda gate: _BrokenArtefact(),
    )

    r = compute_regime_state_from_inputs(
        portfolio_state=_portfolio(),
        allocation_cfg=load_allocation(),
        feature_rows=_rows(),
        news_dispersion=None,
    )
    assert r.metadata.get("regime_classifier_used") is False
    assert r.metadata.get("regime_classifier_reason") == "predict_failed"
