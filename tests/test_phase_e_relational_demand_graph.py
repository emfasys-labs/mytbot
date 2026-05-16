from __future__ import annotations

import json

import pandas as pd

from system.demand_engine import DemandEngine, _load_learned_graph_artifact
from system.relational_demand_graph import (
    RelationalDemandGraphArtifact,
    build_relational_artifact,
    evaluate_relational_shadow,
)
from scripts.report_phase_e_demand_shadow import build_shadow_evidence
from scripts.report_phase_e_relational_graph import summarize_graph


def _close_frame() -> pd.DataFrame:
    idx = pd.date_range("2024-01-01", periods=420, freq="h", tz="UTC")
    src = [100.0]
    dst = [50.0]
    hedge = [80.0]
    for i in range(1, len(idx)):
        shock = 0.002 if i % 5 in (0, 1) else -0.001
        src.append(src[-1] * (1 + shock))
        lag = (src[-2] / src[-3] - 1) if i >= 2 else 0.0
        dst.append(dst[-1] * (1 + lag * 0.8))
        hedge.append(hedge[-1] * (1 - lag * 0.5))
    return pd.DataFrame({"SRC": src, "DST": dst, "HEDGE": hedge}, index=idx)


def _df(a: float, b: float) -> pd.DataFrame:
    idx = pd.date_range("2024-02-01", periods=2, freq="h", tz="UTC")
    return pd.DataFrame({"close": [a, b]}, index=idx)


def test_learn_relational_artifact_finds_lagged_edges() -> None:
    artifact = build_relational_artifact(
        _close_frame(),
        min_overlap=200,
        min_abs_lag_corr=0.05,
        max_edges=10,
    )

    assert artifact.edges
    assert any(e.source == "SRC" and e.target == "DST" for e in artifact.edges)
    assert artifact.metadata["bar_count"] == 420


def test_relational_artifact_roundtrip_and_shadow_eval() -> None:
    artifact = build_relational_artifact(_close_frame(), min_overlap=200, min_abs_lag_corr=0.05, max_edges=10)
    raw = artifact.to_dict()
    loaded = RelationalDemandGraphArtifact.from_dict(raw)

    out = evaluate_relational_shadow(
        loaded,
        {"SRC": _df(100, 101), "DST": _df(50, 50.1)},
    )

    assert out["learned_cross_asset_shadow_edge_count"] == len(artifact.edges)
    assert out["learned_cross_asset_shadow_edges_used"] > 0
    assert -1.0 <= out["learned_cross_asset_shadow_score"] <= 1.0


def test_demand_engine_adds_learned_shadow_components(tmp_path, monkeypatch) -> None:
    artifact = build_relational_artifact(_close_frame(), min_overlap=200, min_abs_lag_corr=0.05, max_edges=10)
    path = tmp_path / "graph.json"
    path.write_text(json.dumps(artifact.to_dict()), encoding="utf-8")
    _load_learned_graph_artifact.cache_clear()

    eng = DemandEngine(
        {
            "enabled": True,
            "learned_graph_shadow": {
                "enabled": True,
                "artifact_path": str(path),
                "scale": 30.0,
            },
        }
    )
    out = eng.compute(ai_result=None, feature_map={"SRC": _df(100, 101), "DST": _df(50, 50.1)})

    assert "learned_cross_asset_shadow_score" in out.components
    assert out.components["learned_cross_asset_shadow_edge_count"] == len(artifact.edges)


def test_phase_e_report_summarizes_graph() -> None:
    artifact = build_relational_artifact(_close_frame(), min_overlap=200, min_abs_lag_corr=0.05, max_edges=10)

    out = summarize_graph(artifact.to_dict())

    assert out["edges"] == len(artifact.edges)
    assert out["symbols"] == 3
    assert out["max_abs_weight"] >= out["avg_abs_weight"]


def test_phase_e_demand_shadow_report_scores_future_panel_returns() -> None:
    close = _close_frame()
    artifact = build_relational_artifact(close, min_overlap=200, min_abs_lag_corr=0.05, max_edges=10)

    summary, rows = build_shadow_evidence(
        close,
        artifact,
        horizon=1,
        scale=30.0,
        demand_config={"risk_on_anchors": ["SRC", "DST"], "risk_off_anchors": ["HEDGE"]},
        signal_threshold=0.0,
    )

    assert rows
    assert summary["observations"] == len(rows)
    assert summary["artifact_edges"] == len(artifact.edges)
    assert "learned_ic" in summary
    assert "static_cross_asset_ic" in summary
    assert summary["recommendation"] in {"do_not_promote", "keep_shadow"}
