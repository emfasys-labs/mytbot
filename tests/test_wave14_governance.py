"""
tests/test_wave14_governance.py
==================================
Wave 14 acceptance tests.

Coverage:

- A fresh ``ActivationContext`` fails closed (every gate red except
  paper_soak when target_status="paper").
- Each gate's specific pass/fail conditions:
    * feature_contract — needs hash + frozen
    * model_registered — needs YAML + DB row + status >= target
    * validation_report — needs file + optional metric ≥ threshold
    * paper_soak — needs >= min_days, no anomalies, skipped for "paper"
    * risk_rejection — needs sign-off + rate <= max
    * execution_cost — p95 within tolerance × expected
    * rollback — documented + tested
    * config_flag — path supplied + value=True
- Aggregate ``cleared_for_activation`` only True when all gates pass.
- ``build_paper_soak_report`` produces all six markdown sections.
- The renderer surfaces strategy-status table from the Wave-13 payload.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from governance.activation_gates import (
    ActivationContext,
    ActivationGate,
    ActivationGates,
    evaluate_activation,
)
from system.paper_soak import build_paper_soak_report


# ── helpers ────────────────────────────────────────────────────────────────


def _ctx_all_pass(target_status: str = "micro_live", *, tmp_path: Path) -> ActivationContext:
    """Build a context where every gate passes."""
    report = tmp_path / "validation.md"
    report.write_text("# valid\n", encoding="utf-8")
    return ActivationContext(
        model_name="m",
        model_version="0.1",
        target_status=target_status,
        feature_contract_hash="abc123",
        feature_contract_frozen=True,
        registered_in_yaml=True,
        registered_in_db=True,
        registry_status="micro_live",
        validation_report_path=report,
        validation_metric_value=0.65,
        validation_metric_threshold=0.55,
        validation_metric_name="hit_rate",
        paper_soak_start=datetime.now(timezone.utc) - timedelta(days=20),
        paper_soak_min_days=14,
        paper_soak_anomalies=[],
        risk_rejection_review_signed_off=True,
        risk_rejection_rate=0.10,
        risk_rejection_rate_max=0.40,
        realised_slippage_bps_p95=8.0,
        expected_slippage_bps=10.0,
        slippage_tolerance_multiplier=1.5,
        rollback_documented=True,
        rollback_test_passed=True,
        config_flag_path="config/strategies.yaml",
        config_flag_value=True,
    )


def _gate_result(verdict, gate: ActivationGate):
    for g in verdict.gates:
        if g.gate is gate:
            return g
    return None


# ── default state fails closed ─────────────────────────────────────────────


def test_fresh_context_fails_every_gate_for_micro_live() -> None:
    ctx = ActivationContext(
        model_name="m", model_version="0.1", target_status="micro_live"
    )
    v = evaluate_activation(ctx)
    assert v.cleared_for_activation is False
    # Every gate failed (paper_soak is required at micro_live).
    assert all(not g.passed for g in v.gates)


def test_fresh_context_for_paper_skips_soak_gate() -> None:
    ctx = ActivationContext(
        model_name="m", model_version="0.1", target_status="paper"
    )
    v = evaluate_activation(ctx)
    soak = _gate_result(v, ActivationGate.PAPER_SOAK)
    assert soak.passed is True
    assert soak.reason == "soak_not_required_for_paper"
    # But other gates still fail.
    assert v.cleared_for_activation is False


# ── individual gates ─────────────────────────────────────────────────────


def test_feature_contract_requires_hash_and_frozen() -> None:
    ctx = ActivationContext(model_name="m", model_version="0.1", feature_contract_hash="abc")
    v = evaluate_activation(ctx)
    res = _gate_result(v, ActivationGate.FEATURE_CONTRACT)
    assert res.passed is False
    assert res.reason == "feature_contract_not_frozen"
    ctx.feature_contract_frozen = True
    v = evaluate_activation(ctx)
    res = _gate_result(v, ActivationGate.FEATURE_CONTRACT)
    assert res.passed is True


def test_model_registered_requires_status_at_or_above_target() -> None:
    ctx = ActivationContext(
        model_name="m", model_version="0.1",
        target_status="micro_live",
        registered_in_yaml=True, registered_in_db=True,
        registry_status="paper",  # below micro_live
    )
    v = evaluate_activation(ctx)
    res = _gate_result(v, ActivationGate.MODEL_REGISTERED)
    assert res.passed is False
    assert res.reason == "registry_status_below_target"


def test_validation_report_metric_below_threshold_blocks(tmp_path: Path) -> None:
    p = tmp_path / "validation.md"
    p.write_text("ok\n", encoding="utf-8")
    ctx = ActivationContext(
        model_name="m", model_version="0.1",
        validation_report_path=p,
        validation_metric_value=0.30,
        validation_metric_threshold=0.55,
        validation_metric_name="hit_rate",
    )
    v = evaluate_activation(ctx)
    res = _gate_result(v, ActivationGate.VALIDATION_REPORT)
    assert res.passed is False
    assert res.reason == "metric_below_threshold"


def test_validation_report_missing_file_blocks(tmp_path: Path) -> None:
    ghost = tmp_path / "does_not_exist.md"
    ctx = ActivationContext(
        model_name="m", model_version="0.1",
        validation_report_path=ghost,
    )
    v = evaluate_activation(ctx)
    res = _gate_result(v, ActivationGate.VALIDATION_REPORT)
    assert res.passed is False
    assert res.reason == "report_file_not_found"


def test_paper_soak_too_short_blocks(tmp_path: Path) -> None:
    ctx = _ctx_all_pass("micro_live", tmp_path=tmp_path)
    ctx.paper_soak_start = datetime.now(timezone.utc) - timedelta(days=3)  # too short
    v = evaluate_activation(ctx)
    res = _gate_result(v, ActivationGate.PAPER_SOAK)
    assert res.passed is False
    assert res.reason == "soak_too_short"


def test_paper_soak_anomalies_present_blocks(tmp_path: Path) -> None:
    ctx = _ctx_all_pass("micro_live", tmp_path=tmp_path)
    ctx.paper_soak_anomalies = ["unexplained_drawdown_2026-04-15"]
    v = evaluate_activation(ctx)
    res = _gate_result(v, ActivationGate.PAPER_SOAK)
    assert res.passed is False
    assert res.reason == "soak_anomalies_present"


def test_risk_rejection_rate_too_high_blocks(tmp_path: Path) -> None:
    ctx = _ctx_all_pass("micro_live", tmp_path=tmp_path)
    ctx.risk_rejection_rate = 0.55  # above 40% default
    v = evaluate_activation(ctx)
    res = _gate_result(v, ActivationGate.RISK_REJECTION)
    assert res.passed is False
    assert res.reason == "rejection_rate_too_high"


def test_execution_cost_outside_tolerance_blocks(tmp_path: Path) -> None:
    ctx = _ctx_all_pass("micro_live", tmp_path=tmp_path)
    ctx.realised_slippage_bps_p95 = 25.0  # 2.5× expected
    ctx.expected_slippage_bps = 10.0
    v = evaluate_activation(ctx)
    res = _gate_result(v, ActivationGate.EXECUTION_COST)
    assert res.passed is False
    assert res.reason == "realised_slippage_exceeds_tolerance"


def test_rollback_test_not_run_blocks(tmp_path: Path) -> None:
    ctx = _ctx_all_pass("micro_live", tmp_path=tmp_path)
    ctx.rollback_test_passed = False
    v = evaluate_activation(ctx)
    res = _gate_result(v, ActivationGate.ROLLBACK)
    assert res.passed is False


def test_config_flag_off_blocks(tmp_path: Path) -> None:
    ctx = _ctx_all_pass("micro_live", tmp_path=tmp_path)
    ctx.config_flag_value = False
    v = evaluate_activation(ctx)
    res = _gate_result(v, ActivationGate.CONFIG_FLAG)
    assert res.passed is False
    assert res.reason == "config_flag_not_enabled"


# ── aggregate verdict ─────────────────────────────────────────────────────


def test_full_pass_clears_activation(tmp_path: Path) -> None:
    ctx = _ctx_all_pass("micro_live", tmp_path=tmp_path)
    v = evaluate_activation(ctx)
    assert v.cleared_for_activation is True
    assert v.failed_gates == []


def test_one_failure_blocks_activation(tmp_path: Path) -> None:
    ctx = _ctx_all_pass("micro_live", tmp_path=tmp_path)
    ctx.feature_contract_frozen = False
    v = evaluate_activation(ctx)
    assert v.cleared_for_activation is False
    assert len(v.failed_gates) == 1
    assert v.failed_gates[0].gate is ActivationGate.FEATURE_CONTRACT


def test_verdict_to_dict_structure(tmp_path: Path) -> None:
    ctx = _ctx_all_pass("micro_live", tmp_path=tmp_path)
    d = evaluate_activation(ctx).to_dict()
    assert d["model_name"] == "m"
    assert d["cleared_for_activation"] is True
    assert isinstance(d["gates"], list)
    assert all("gate" in g and "passed" in g for g in d["gates"])


# ── paper-soak report builder ────────────────────────────────────────────


def _stub_payload() -> dict:
    return {
        "schema": "wave13_dashboard",
        "version": 1,
        "funnel": {
            "aggregate": {
                "evaluated": 100, "generated": 50,
                "meta_label_kept": 40, "meta_label_blocked": 10,
                "forecast_kept": 38, "forecast_blocked": 2,
                "risk_approved": 30, "risk_rejected": 8,
                "execution_approved": 28, "execution_blocked": 2,
                "executed": 25,
            },
            "per_strategy": {},
        },
        "strategy_status": {
            "momentum": {
                "status": "trading",
                "evaluated": 100,
                "generated": 50,
                "executed": 25,
                "model_blocks": 12,
                "risk_rejections": 8,
                "execution_blocks": 2,
            },
        },
        "strategy_coverage": {
            "families": [
                {"name": "factor_sleeve", "wave": 3, "enabled": True, "paper_only": True},
            ],
        },
        "model_health": {
            "registry_path": "config/model_registry.yaml",
            "registered_models": [
                {
                    "name": "meta",
                    "version": "0.1.0",
                    "task": "classification",
                    "target": "triple_barrier",
                    "approval_status": "paper",
                    "calibration_method": "isotonic",
                    "feature_contract_hash": "abcdef0123456789",
                }
            ],
            "feature_freshness_seconds": 5.0,
            "stale_model_warning": False,
            "last_prediction_count": 1234,
        },
        "portfolio_intelligence": {
            "available": True,
            "gross_exposure_target": "0.5",
            "net_exposure_target": "0.5",
            "wave8_vol_overlay_used": True,
            "wave8_vol_overlay_scale": 0.85,
            "demand_score": 0.2,
            "demand_trend": "rising",
        },
        "execution_intelligence": {
            "wave9_gate_passed": 28,
            "wave9_gate_blocked": 2,
            "execution_block_rate": 0.0667,
        },
    }


def test_paper_soak_report_emits_six_sections() -> None:
    rep = build_paper_soak_report(
        dashboard_payload=_stub_payload(),
        model_name="meta",
        model_version="0.1.0",
        soak_started_at=datetime.now(timezone.utc) - timedelta(days=15),
    )
    expected = {
        "model_health.md",
        "strategy_attribution.md",
        "execution_quality.md",
        "risk_rejections.md",
        "d015_replacement_behaviour.md",
        "drawdown_report.md",
    }
    assert set(rep.sections.keys()) == expected
    # Each section is non-empty markdown.
    for k, body in rep.sections.items():
        assert body.startswith("##"), f"{k} should start with a markdown heading"
        assert len(body) > 50, f"{k} suspiciously short"


def test_paper_soak_report_renders_strategy_status_in_attribution() -> None:
    rep = build_paper_soak_report(dashboard_payload=_stub_payload())
    body = rep.sections["strategy_attribution.md"]
    assert "momentum" in body
    assert "trading" in body


def test_paper_soak_report_records_elapsed_days() -> None:
    started = datetime.now(timezone.utc) - timedelta(days=10)
    rep = build_paper_soak_report(
        dashboard_payload=_stub_payload(), soak_started_at=started
    )
    assert 9.0 < rep.soak_days_elapsed < 11.0


def test_paper_soak_report_handles_no_drawdown_metrics() -> None:
    rep = build_paper_soak_report(dashboard_payload=_stub_payload())
    body = rep.sections["drawdown_report.md"]
    assert "no drawdown metrics supplied" in body


def test_paper_soak_report_includes_risk_rejection_breakdown() -> None:
    rep = build_paper_soak_report(
        dashboard_payload=_stub_payload(),
        risk_rejection_breakdown={"max_exposure": 5, "drawdown_limit": 3},
    )
    body = rep.sections["risk_rejections.md"]
    assert "max_exposure" in body
    assert "drawdown_limit" in body
