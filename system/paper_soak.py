"""
system/paper_soak.py
======================
Wave 14 — paper-soak report builder.

Aggregates the runtime data the operator needs at the end of a paper
soak window into a single ``PaperSoakReport`` plus six per-section
markdown bodies that match the reports the plan calls for:

    reports/paper_soak/
        model_health.md
        strategy_attribution.md
        execution_quality.md
        risk_rejections.md
        d015_replacement_behaviour.md
        drawdown_report.md

The renderer is pure Python — no template engine required.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Optional


@dataclass
class PaperSoakReport:
    generated_at: datetime
    model_name: Optional[str]
    model_version: Optional[str]
    soak_started_at: Optional[datetime]
    soak_days_elapsed: float
    sections: dict[str, str] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


# ── helpers ────────────────────────────────────────────────────────────────


def _h(line: str, level: int = 2) -> str:
    return f"{'#' * level} {line}\n\n"


def _kv(label: str, value: Any) -> str:
    return f"- **{label}**: {value}\n"


def _na(value: Any, default: str = "—") -> str:
    if value is None:
        return default
    return str(value)


# ── per-section renderers ─────────────────────────────────────────────────


def render_model_health(*, dashboard_payload: Mapping[str, Any]) -> str:
    body = _h("Model health")
    mh = dashboard_payload.get("model_health") or {}
    body += _kv("registry_path", mh.get("registry_path"))
    body += _kv("feature_freshness_seconds", _na(mh.get("feature_freshness_seconds")))
    body += _kv("stale_model_warning", mh.get("stale_model_warning"))
    body += _kv("last_prediction_count", _na(mh.get("last_prediction_count")))
    body += "\n" + _h("Registered models", level=3)
    rows = mh.get("registered_models") or []
    if not rows:
        body += "_no models registered_\n"
        return body
    body += "| name | version | task | target | status | calibration | feature_hash |\n"
    body += "|---|---|---|---|---|---|---|\n"
    for r in rows:
        body += (
            f"| {r.get('name')} | {r.get('version')} | {r.get('task')} | "
            f"{r.get('target')} | {r.get('approval_status')} | "
            f"{r.get('calibration_method')} | {(r.get('feature_contract_hash') or '')[:12]} |\n"
        )
    return body


def render_strategy_attribution(*, dashboard_payload: Mapping[str, Any]) -> str:
    body = _h("Strategy attribution")
    funnel = dashboard_payload.get("funnel") or {}
    statuses = dashboard_payload.get("strategy_status") or {}
    coverage = dashboard_payload.get("strategy_coverage") or {}

    body += _h("Coverage", level=3)
    body += "| family | wave | enabled | paper_only |\n|---|---|---|---|\n"
    for fam in coverage.get("families", []):
        body += (
            f"| {fam.get('name')} | {fam.get('wave')} | {fam.get('enabled')} | "
            f"{fam.get('paper_only', '—')} |\n"
        )

    body += "\n" + _h("Per-strategy funnel", level=3)
    if not statuses:
        body += "_no per-strategy data recorded_\n"
        return body
    body += "| strategy | status | evaluated | generated | model_blocks | risk_rej | exec_blocks | executed |\n"
    body += "|---|---|---|---|---|---|---|---|\n"
    for sym, s in sorted(statuses.items()):
        body += (
            f"| {sym} | {s.get('status')} | {s.get('evaluated', 0)} | "
            f"{s.get('generated', 0)} | {s.get('model_blocks', 0)} | "
            f"{s.get('risk_rejections', 0)} | {s.get('execution_blocks', 0)} | "
            f"{s.get('executed', 0)} |\n"
        )

    body += "\n" + _h("Aggregate funnel", level=3)
    agg = funnel.get("aggregate") or {}
    for k in (
        "evaluated", "generated",
        "meta_label_kept", "meta_label_blocked",
        "forecast_kept", "forecast_blocked",
        "risk_approved", "risk_rejected",
        "execution_approved", "execution_blocked",
        "executed",
    ):
        body += _kv(k, agg.get(k, 0))
    return body


def render_execution_quality(*, dashboard_payload: Mapping[str, Any]) -> str:
    body = _h("Execution quality")
    ei = dashboard_payload.get("execution_intelligence") or {}
    body += _kv("wave9_gate_passed", ei.get("wave9_gate_passed", 0))
    body += _kv("wave9_gate_blocked", ei.get("wave9_gate_blocked", 0))
    body += _kv("execution_block_rate", round(float(ei.get("execution_block_rate", 0.0)), 4))
    body += "\n_Calibrate `execution_models.urgency_policy` if block rate "
    body += "is materially different from expectation._\n"
    return body


def render_risk_rejections(
    *,
    dashboard_payload: Mapping[str, Any],
    risk_rejection_breakdown: Optional[Mapping[str, int]] = None,
) -> str:
    body = _h("Risk rejections")
    funnel = dashboard_payload.get("funnel") or {}
    agg = funnel.get("aggregate") or {}
    approved = int(agg.get("risk_approved", 0))
    rejected = int(agg.get("risk_rejected", 0))
    total = approved + rejected
    rate = (rejected / total) if total > 0 else 0.0
    body += _kv("risk_approved", approved)
    body += _kv("risk_rejected", rejected)
    body += _kv("risk_rejection_rate", round(rate, 4))
    if risk_rejection_breakdown:
        body += "\n" + _h("Reasons", level=3)
        body += "| reason | count |\n|---|---|\n"
        for reason, n in sorted(risk_rejection_breakdown.items(), key=lambda kv: -kv[1]):
            body += f"| {reason} | {n} |\n"
    return body


def render_d015_replacement_behaviour(
    *,
    dashboard_payload: Mapping[str, Any],
    replacement_metrics: Optional[Mapping[str, Any]] = None,
) -> str:
    body = _h("D015 replacement behaviour")
    pi = dashboard_payload.get("portfolio_intelligence") or {}
    body += _kv("gross_exposure_target", pi.get("gross_exposure_target"))
    body += _kv("net_exposure_target", pi.get("net_exposure_target"))
    body += _kv("capital_deployment_target", pi.get("capital_deployment_target"))
    body += _kv("wave8_vol_overlay_used", pi.get("wave8_vol_overlay_used"))
    body += _kv("wave8_vol_overlay_scale", pi.get("wave8_vol_overlay_scale"))
    body += _kv("demand_score", pi.get("demand_score"))
    body += _kv("demand_trend", pi.get("demand_trend"))
    if replacement_metrics:
        body += "\n" + _h("Replacement metrics", level=3)
        for k, v in replacement_metrics.items():
            body += _kv(k, v)
    return body


def render_drawdown_report(
    *,
    drawdown_metrics: Optional[Mapping[str, Any]] = None,
) -> str:
    body = _h("Drawdown report")
    if not drawdown_metrics:
        body += "_no drawdown metrics supplied_\n"
        return body
    for k, v in drawdown_metrics.items():
        body += _kv(k, v)
    return body


# ── orchestration ─────────────────────────────────────────────────────────


def build_paper_soak_report(
    *,
    dashboard_payload: Mapping[str, Any],
    model_name: Optional[str] = None,
    model_version: Optional[str] = None,
    soak_started_at: Optional[datetime] = None,
    risk_rejection_breakdown: Optional[Mapping[str, int]] = None,
    replacement_metrics: Optional[Mapping[str, Any]] = None,
    drawdown_metrics: Optional[Mapping[str, Any]] = None,
) -> PaperSoakReport:
    now = datetime.now(timezone.utc)
    days = (
        (now - soak_started_at).total_seconds() / 86_400.0
        if soak_started_at is not None
        else 0.0
    )

    sections = {
        "model_health.md": render_model_health(dashboard_payload=dashboard_payload),
        "strategy_attribution.md": render_strategy_attribution(dashboard_payload=dashboard_payload),
        "execution_quality.md": render_execution_quality(dashboard_payload=dashboard_payload),
        "risk_rejections.md": render_risk_rejections(
            dashboard_payload=dashboard_payload,
            risk_rejection_breakdown=risk_rejection_breakdown,
        ),
        "d015_replacement_behaviour.md": render_d015_replacement_behaviour(
            dashboard_payload=dashboard_payload,
            replacement_metrics=replacement_metrics,
        ),
        "drawdown_report.md": render_drawdown_report(drawdown_metrics=drawdown_metrics),
    }

    return PaperSoakReport(
        generated_at=now,
        model_name=model_name,
        model_version=model_version,
        soak_started_at=soak_started_at,
        soak_days_elapsed=round(days, 2),
        sections=sections,
        metadata={"funnel": dashboard_payload.get("funnel")},
    )
