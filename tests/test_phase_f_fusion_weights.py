"""Phase F: learned regime-conditional fusion weights — shadow + governance.

Pure/torch-free. Verifies the shadow score math, the governance gate
(unpromoted artifact refused on the live-path load), and inert-by-default.
"""

from __future__ import annotations

import json

from system.fusion_weights import (
    COMPONENTS,
    RegimeConditionalFusionWeights,
    fusion_weights_shadow_enabled,
)


def _comps(v: float = 0.5) -> dict[str, float]:
    return {c: v for c in COMPONENTS}


def test_shadow_score_is_weighted_mean_in_unit_interval() -> None:
    w = RegimeConditionalFusionWeights(
        by_regime={"trend_up": {c: 0.0 for c in COMPONENTS} | {"momentum": 1.0}},
        default={c: 1.0 for c in COMPONENTS},
    )
    # trend_up uses only momentum
    comp = _comps(0.0)
    comp["momentum"] = 0.8
    assert abs(w.shadow_score(comp, "trend_up") - 0.8) < 1e-9
    # unknown regime → default (equal) → mean
    assert abs(w.shadow_score(_comps(0.4), "no_such_regime") - 0.4) < 1e-9
    # clamped to [0,1] and never raises on junk
    assert w.shadow_score({}, "trend_up") == 0.0 or w.shadow_score({}, "trend_up") is not None
    assert w.shadow_score({"momentum": 99}, "trend_up") == 1.0


def test_empty_weightset_returns_none_safely() -> None:
    w = RegimeConditionalFusionWeights(by_regime={}, default={})
    assert w.shadow_score(_comps(), "anything") is None


def test_load_refuses_unpromoted_on_live_path(tmp_path) -> None:
    p = tmp_path / "wf.json"
    RegimeConditionalFusionWeights(
        default={c: 1.0 for c in COMPONENTS},
        metadata={"promote_eligible": False},
    ).save(p)
    # Live-path default (require_promote=True) MUST refuse.
    assert RegimeConditionalFusionWeights.load(p) is None
    # Shadow/eval path may still observe it.
    assert RegimeConditionalFusionWeights.load(p, require_promote=False) is not None


def test_load_allows_promoted_artifact(tmp_path) -> None:
    p = tmp_path / "wf.json"
    RegimeConditionalFusionWeights(
        default={c: 1.0 for c in COMPONENTS},
        metadata={"promote_eligible": True},
    ).save(p)
    got = RegimeConditionalFusionWeights.load(p)
    assert got is not None and got.metadata["promote_eligible"] is True


def test_missing_artifact_is_inert() -> None:
    assert RegimeConditionalFusionWeights.load("does/not/exist.json") is None


def test_shadow_flag_default_off(monkeypatch) -> None:
    monkeypatch.delenv("FUSION_WEIGHTS_SHADOW", raising=False)
    assert fusion_weights_shadow_enabled() is False
    monkeypatch.setenv("FUSION_WEIGHTS_SHADOW", "1")
    assert fusion_weights_shadow_enabled() is True
