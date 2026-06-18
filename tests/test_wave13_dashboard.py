"""
tests/test_wave13_dashboard.py
================================
Wave 13 acceptance tests for the dashboard observability payload.

Coverage:

- ``FunnelTelemetry.record_*`` accumulates per-strategy and aggregate
  counters; ``snapshot`` returns a copy that's immune to mutation.
- ``build_wave13_payload`` distinguishes the four block reasons:
  blocked_by_model / blocked_by_risk / blocked_by_execution / no_signal.
- The payload's ``strategy_coverage`` reflects the YAML gates landed
  in waves 2/3/5/12.
- ``model_health.stale_model_warning`` triggers when feature freshness
  exceeds 60s.
- ``execution_intelligence.execution_block_rate`` is computed correctly
  and degrades gracefully when no orders have been seen.
- The new ``/dashboard/wave13`` API endpoint returns the payload
  shape (smoke test against the FastAPI TestClient).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from api.wave13_dashboard import (
    _classify_strategy_status,
    build_wave13_payload,
)
from system.funnel_telemetry import (
    FunnelTelemetry,
    get_default_funnel_telemetry,
    record_strategy_candidate_rows,
    reset_default_funnel_telemetry,
)


# ── funnel telemetry ──────────────────────────────────────────────────────


def test_funnel_records_and_aggregates() -> None:
    f = FunnelTelemetry()
    f.record_evaluated("momentum", 5)
    f.record_generated("momentum", 3)
    f.record_meta_label_blocked("momentum", 1)
    f.record_risk_rejected("momentum", 1)
    f.record_executed("momentum", 1)
    snap = f.snapshot()
    d = snap.to_dict()
    assert d["aggregate"]["evaluated"] == 5
    assert d["aggregate"]["generated"] == 3
    assert d["aggregate"]["executed"] == 1
    assert d["per_strategy"]["momentum"]["meta_label_blocked"] == 1


def test_funnel_snapshot_is_isolated_copy() -> None:
    f = FunnelTelemetry()
    f.record_evaluated("a", 1)
    s1 = f.snapshot()
    f.record_evaluated("a", 1)
    s2 = f.snapshot()
    assert s1.evaluated["a"] == 1
    assert s2.evaluated["a"] == 2


def test_funnel_default_singleton_reset() -> None:
    reset_default_funnel_telemetry()
    f = get_default_funnel_telemetry()
    f.record_executed("x", 7)
    assert f.snapshot().to_dict()["aggregate"]["executed"] == 7
    reset_default_funnel_telemetry()
    assert f.snapshot().to_dict()["aggregate"]["executed"] == 0


def test_strategy_candidate_rows_feed_funnel() -> None:
    f = FunnelTelemetry()
    record_strategy_candidate_rows(
        [
            {"strategy": "momentum", "status": "generated"},
            {"strategy": "momentum", "status": "filtered_meta"},
            {"strategy": "mean_reversion", "status": "no_setup"},
        ],
        funnel=f,
    )
    d = f.snapshot().to_dict()
    assert d["aggregate"]["evaluated"] == 3
    assert d["per_strategy"]["momentum"]["generated"] == 1
    assert d["per_strategy"]["momentum"]["meta_label_blocked"] == 1


# ── strategy-status classification ────────────────────────────────────────


def test_classify_blocked_by_model() -> None:
    out = _classify_strategy_status({
        "momentum": {"evaluated": 10, "generated": 5, "meta_label_blocked": 3, "executed": 0}
    })
    assert out["momentum"]["status"] == "blocked_by_model"


def test_classify_blocked_by_risk() -> None:
    out = _classify_strategy_status({
        "momentum": {"evaluated": 10, "generated": 5, "risk_rejected": 4, "executed": 0}
    })
    assert out["momentum"]["status"] == "blocked_by_risk"


def test_classify_blocked_by_execution() -> None:
    out = _classify_strategy_status({
        "momentum": {"evaluated": 10, "generated": 5, "execution_blocked": 4, "executed": 0}
    })
    assert out["momentum"]["status"] == "blocked_by_execution"


def test_classify_no_signal_when_evaluated_but_no_generation() -> None:
    out = _classify_strategy_status({
        "momentum": {"evaluated": 10, "generated": 0, "executed": 0}
    })
    assert out["momentum"]["status"] == "no_signal"


def test_classify_idle_when_nothing_evaluated() -> None:
    out = _classify_strategy_status({
        "momentum": {"evaluated": 0, "generated": 0, "executed": 0}
    })
    assert out["momentum"]["status"] == "idle"


def test_classify_trading_when_executed() -> None:
    out = _classify_strategy_status({
        "momentum": {"evaluated": 10, "generated": 5, "executed": 3}
    })
    assert out["momentum"]["status"] == "trading"


# ── build_wave13_payload ────────────────────────────────────────────────


def test_payload_shape_with_default_inputs() -> None:
    reset_default_funnel_telemetry()
    payload = build_wave13_payload()
    expected_keys = {
        "schema",
        "version",
        "generated_at",
        "funnel",
        "strategy_status",
        "strategy_coverage",
        "model_health",
        "portfolio_intelligence",
        "execution_intelligence",
    }
    assert expected_keys.issubset(set(payload.keys()))
    assert payload["schema"] == "wave13_dashboard"
    assert payload["version"] == 1


def test_payload_strategy_coverage_reflects_yaml_state() -> None:
    payload = build_wave13_payload()
    coverage = payload["strategy_coverage"]
    families = {f["name"]: f for f in coverage["families"]}
    # Paper mode exercises all paper-safe advanced sleeves; live activation
    # remains governed by paper_only flags and model/strategy gates.
    # D163 — the TRAINED meta-labeler is deliberately SHADOWED (disabled)
    # pending re-validation against the clean profitability baseline; the
    # heuristic meta-labeler stays active. Reflect that deliberate state.
    assert families["trained_meta_labeler"]["enabled"] is False
    assert families["factor_sleeve"]["enabled"] is True
    assert families["stat_arb_pairs"]["enabled"] is True
    assert families["options_directional"]["enabled"] is True
    assert families["options_hedging"]["enabled"] is True
    assert families["options_directional"]["paper_only"] is True
    assert families["options_hedging"]["paper_only"] is True


def test_payload_model_health_stale_warning_triggers() -> None:
    fresh = build_wave13_payload(feature_freshness_seconds=10.0)
    stale = build_wave13_payload(feature_freshness_seconds=300.0)
    assert fresh["model_health"]["stale_model_warning"] is False
    assert stale["model_health"]["stale_model_warning"] is True


def test_payload_execution_intelligence_block_rate() -> None:
    p = build_wave13_payload(wave9_gate_passed=8, wave9_gate_blocked=2)
    assert p["execution_intelligence"]["execution_block_rate"] == pytest.approx(0.2)
    p_zero = build_wave13_payload(wave9_gate_passed=0, wave9_gate_blocked=0)
    assert p_zero["execution_intelligence"]["execution_block_rate"] == 0.0


def test_payload_portfolio_intelligence_unavailable_when_no_snapshot() -> None:
    p = build_wave13_payload(snapshot=None)
    assert p["portfolio_intelligence"]["available"] is False


def test_payload_portfolio_intelligence_surfaces_overlay_metadata() -> None:
    snap = {
        "gross_exposure_target": "0.5",
        "net_exposure_target": "0.5",
        "capital_deployment_target": "50000",
        "metadata": {
            "wave8_vol_overlay_used": True,
            "wave8_vol_overlay_scale": 0.6,
            "wave8_vol_overlay_drawdown": 0.10,
            "demand_score": 0.3,
            "demand_trend": "rising",
        },
        "allocation_targets": [
            {
                "symbol": "AAPL",
                "target_weight": "0.20",
                "target_notional": "20000",
                "side": "long",
                "strategy_name": "momentum",
            }
        ],
    }
    p = build_wave13_payload(snapshot=snap)
    pi = p["portfolio_intelligence"]
    assert pi["available"] is True
    assert pi["wave8_vol_overlay_used"] is True
    assert pi["wave8_vol_overlay_scale"] == 0.6
    assert pi["target_weights"][0]["symbol"] == "AAPL"
    assert pi["demand_trend"] == "rising"


def test_payload_funnel_drives_strategy_status(monkeypatch) -> None:
    reset_default_funnel_telemetry()
    f = get_default_funnel_telemetry()
    f.record_evaluated("momentum", 10)
    f.record_generated("momentum", 5)
    f.record_meta_label_blocked("momentum", 5)
    p = build_wave13_payload()
    assert p["strategy_status"]["momentum"]["status"] == "blocked_by_model"
    reset_default_funnel_telemetry()


# ── /dashboard/wave13 endpoint smoke ──────────────────────────────────────


@pytest.mark.asyncio
async def test_dashboard_wave13_endpoint_returns_payload() -> None:
    from fastapi.testclient import TestClient

    from api.server import app

    reset_default_funnel_telemetry()
    f = get_default_funnel_telemetry()
    f.record_evaluated("smoke_strategy", 3)
    f.record_executed("smoke_strategy", 1)

    with TestClient(app) as client:
        resp = client.get("/dashboard/wave13")
    assert resp.status_code == 200
    body = resp.json()
    assert body.get("schema") == "wave13_dashboard"
    assert "smoke_strategy" in (body.get("strategy_status") or {})
    reset_default_funnel_telemetry()
