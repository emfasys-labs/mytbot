"""
governance/activation_gates.py
================================
Wave 14 — activation gates.

The strategy/AI roadmap lists eight rules a model or strategy MUST
satisfy before being promoted past ``research`` status:

    1. feature contract is stable
    2. model is registered
    3. validation report exists
    4. paper soak is complete (2-4 weeks)
    5. risk rejection behaviour is understood
    6. execution cost is acceptable
    7. rollback path exists
    8. config flag controls activation

This module encodes each rule as a callable gate that takes an
``ActivationContext`` and returns ``pass / fail / reason``. The
aggregate ``ActivationVerdict.cleared_for_activation`` is True only
when every gate passes.

Default state: a fresh ``ActivationContext`` fails most gates. The
operator must affirmatively populate each field — there is no way to
bypass.

The gates are runtime checks, not training-time checks. They run
when a deployment script or admin endpoint asks "is this model okay
to run live now?".
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Optional


# ── inputs ─────────────────────────────────────────────────────────────────


@dataclass
class ActivationContext:
    """
    Everything the gates need to decide. Each field defaults to the
    'unsafe' value so a fresh context fails closed.
    """

    model_name: str
    model_version: str
    target_status: str = "paper"  # "paper" | "micro_live" | "live"

    # Gate 1 — feature contract stability.
    feature_contract_hash: Optional[str] = None
    feature_contract_frozen: bool = False

    # Gate 2 — model registration.
    registered_in_yaml: bool = False
    registered_in_db: bool = False
    registry_status: Optional[str] = None  # "research" | "paper" | "micro_live" | ...

    # Gate 3 — validation report.
    validation_report_path: Optional[Path] = None
    validation_metric_value: Optional[float] = None
    validation_metric_threshold: Optional[float] = None
    validation_metric_name: Optional[str] = None

    # Gate 4 — paper soak window.
    paper_soak_start: Optional[datetime] = None
    paper_soak_min_days: int = 14
    paper_soak_anomalies: list[str] = field(default_factory=list)

    # Gate 5 — risk-rejection behaviour understood.
    risk_rejection_review_signed_off: bool = False
    risk_rejection_rate: Optional[float] = None
    risk_rejection_rate_max: float = 0.40   # block if > 40% of signals are rejected

    # Gate 6 — execution cost acceptable.
    realised_slippage_bps_p95: Optional[float] = None
    expected_slippage_bps: Optional[float] = None
    slippage_tolerance_multiplier: float = 1.5

    # Gate 7 — rollback path.
    rollback_documented: bool = False
    rollback_test_passed: bool = False

    # Gate 8 — config flag controls activation.
    config_flag_path: Optional[str] = None
    config_flag_value: Optional[bool] = None

    # Free-form metadata / notes.
    metadata: dict = field(default_factory=dict)


# ── results ────────────────────────────────────────────────────────────────


class ActivationGate(str, Enum):
    FEATURE_CONTRACT = "feature_contract"
    MODEL_REGISTERED = "model_registered"
    VALIDATION_REPORT = "validation_report"
    PAPER_SOAK = "paper_soak"
    RISK_REJECTION = "risk_rejection"
    EXECUTION_COST = "execution_cost"
    ROLLBACK = "rollback"
    CONFIG_FLAG = "config_flag"


@dataclass(frozen=True)
class ActivationGateResult:
    gate: ActivationGate
    passed: bool
    reason: str
    details: dict = field(default_factory=dict)


@dataclass
class ActivationVerdict:
    model_name: str
    model_version: str
    target_status: str
    gates: list[ActivationGateResult]
    cleared_for_activation: bool

    @property
    def failed_gates(self) -> list[ActivationGateResult]:
        return [g for g in self.gates if not g.passed]

    def to_dict(self) -> dict:
        return {
            "model_name": self.model_name,
            "model_version": self.model_version,
            "target_status": self.target_status,
            "cleared_for_activation": self.cleared_for_activation,
            "gates": [
                {
                    "gate": g.gate.value,
                    "passed": g.passed,
                    "reason": g.reason,
                    "details": dict(g.details),
                }
                for g in self.gates
            ],
        }


# ── individual gate implementations ────────────────────────────────────────


def _gate_feature_contract(ctx: ActivationContext) -> ActivationGateResult:
    if not ctx.feature_contract_hash:
        return ActivationGateResult(
            ActivationGate.FEATURE_CONTRACT, False, "missing_feature_contract_hash"
        )
    if not ctx.feature_contract_frozen:
        return ActivationGateResult(
            ActivationGate.FEATURE_CONTRACT, False, "feature_contract_not_frozen"
        )
    return ActivationGateResult(
        ActivationGate.FEATURE_CONTRACT,
        True,
        "feature_contract_frozen",
        details={"hash": ctx.feature_contract_hash},
    )


def _gate_model_registered(ctx: ActivationContext) -> ActivationGateResult:
    if not ctx.registered_in_yaml:
        return ActivationGateResult(
            ActivationGate.MODEL_REGISTERED, False, "missing_yaml_registration"
        )
    if not ctx.registered_in_db:
        return ActivationGateResult(
            ActivationGate.MODEL_REGISTERED, False, "missing_db_row"
        )
    # Status must be at least "paper" when promoting to paper, etc.
    rank = {"research": 0, "paper": 1, "micro_live": 2, "live": 3, "retired": -1}
    have = rank.get((ctx.registry_status or "research").lower(), 0)
    want = rank.get((ctx.target_status or "paper").lower(), 1)
    if have < want:
        return ActivationGateResult(
            ActivationGate.MODEL_REGISTERED,
            False,
            "registry_status_below_target",
            details={"registry_status": ctx.registry_status, "target_status": ctx.target_status},
        )
    return ActivationGateResult(
        ActivationGate.MODEL_REGISTERED,
        True,
        "registered",
        details={"registry_status": ctx.registry_status},
    )


def _gate_validation_report(ctx: ActivationContext) -> ActivationGateResult:
    if ctx.validation_report_path is None:
        return ActivationGateResult(
            ActivationGate.VALIDATION_REPORT, False, "missing_report_path"
        )
    p = Path(ctx.validation_report_path)
    if not p.exists():
        return ActivationGateResult(
            ActivationGate.VALIDATION_REPORT, False, "report_file_not_found",
            details={"path": str(p)},
        )
    if (
        ctx.validation_metric_value is not None
        and ctx.validation_metric_threshold is not None
    ):
        if not math.isfinite(ctx.validation_metric_value):
            return ActivationGateResult(
                ActivationGate.VALIDATION_REPORT, False, "metric_not_finite"
            )
        if ctx.validation_metric_value < ctx.validation_metric_threshold:
            return ActivationGateResult(
                ActivationGate.VALIDATION_REPORT,
                False,
                "metric_below_threshold",
                details={
                    "metric": ctx.validation_metric_name,
                    "value": ctx.validation_metric_value,
                    "threshold": ctx.validation_metric_threshold,
                },
            )
    return ActivationGateResult(
        ActivationGate.VALIDATION_REPORT,
        True,
        "report_present",
        details={"path": str(p)},
    )


def _gate_paper_soak(ctx: ActivationContext) -> ActivationGateResult:
    if ctx.target_status == "paper":
        # Promotion *to* paper does not need a paper soak window.
        return ActivationGateResult(
            ActivationGate.PAPER_SOAK, True, "soak_not_required_for_paper"
        )
    if ctx.paper_soak_start is None:
        return ActivationGateResult(
            ActivationGate.PAPER_SOAK, False, "no_soak_start_recorded"
        )
    elapsed = (datetime.now(timezone.utc) - ctx.paper_soak_start).total_seconds()
    days = elapsed / 86_400.0
    if days < ctx.paper_soak_min_days:
        return ActivationGateResult(
            ActivationGate.PAPER_SOAK,
            False,
            "soak_too_short",
            details={"days_elapsed": round(days, 2), "min_days": ctx.paper_soak_min_days},
        )
    if ctx.paper_soak_anomalies:
        return ActivationGateResult(
            ActivationGate.PAPER_SOAK,
            False,
            "soak_anomalies_present",
            details={"anomalies": list(ctx.paper_soak_anomalies)},
        )
    return ActivationGateResult(
        ActivationGate.PAPER_SOAK,
        True,
        "soak_complete",
        details={"days_elapsed": round(days, 2)},
    )


def _gate_risk_rejection(ctx: ActivationContext) -> ActivationGateResult:
    if not ctx.risk_rejection_review_signed_off:
        return ActivationGateResult(
            ActivationGate.RISK_REJECTION, False, "review_not_signed_off"
        )
    if ctx.risk_rejection_rate is not None:
        if ctx.risk_rejection_rate > ctx.risk_rejection_rate_max:
            return ActivationGateResult(
                ActivationGate.RISK_REJECTION,
                False,
                "rejection_rate_too_high",
                details={
                    "rate": ctx.risk_rejection_rate,
                    "max": ctx.risk_rejection_rate_max,
                },
            )
    return ActivationGateResult(
        ActivationGate.RISK_REJECTION,
        True,
        "review_signed_off",
        details={"rate": ctx.risk_rejection_rate},
    )


def _gate_execution_cost(ctx: ActivationContext) -> ActivationGateResult:
    if ctx.realised_slippage_bps_p95 is None or ctx.expected_slippage_bps is None:
        return ActivationGateResult(
            ActivationGate.EXECUTION_COST, False, "execution_metrics_missing"
        )
    if ctx.realised_slippage_bps_p95 > ctx.expected_slippage_bps * ctx.slippage_tolerance_multiplier:
        return ActivationGateResult(
            ActivationGate.EXECUTION_COST,
            False,
            "realised_slippage_exceeds_tolerance",
            details={
                "p95_bps": ctx.realised_slippage_bps_p95,
                "expected_bps": ctx.expected_slippage_bps,
                "multiplier": ctx.slippage_tolerance_multiplier,
            },
        )
    return ActivationGateResult(
        ActivationGate.EXECUTION_COST,
        True,
        "within_tolerance",
        details={
            "p95_bps": ctx.realised_slippage_bps_p95,
            "expected_bps": ctx.expected_slippage_bps,
        },
    )


def _gate_rollback(ctx: ActivationContext) -> ActivationGateResult:
    if not ctx.rollback_documented:
        return ActivationGateResult(
            ActivationGate.ROLLBACK, False, "rollback_path_undocumented"
        )
    if not ctx.rollback_test_passed:
        return ActivationGateResult(
            ActivationGate.ROLLBACK, False, "rollback_test_not_run_or_failed"
        )
    return ActivationGateResult(ActivationGate.ROLLBACK, True, "rollback_ready")


def _gate_config_flag(ctx: ActivationContext) -> ActivationGateResult:
    if not ctx.config_flag_path:
        return ActivationGateResult(
            ActivationGate.CONFIG_FLAG, False, "no_config_flag_referenced"
        )
    if ctx.config_flag_value is None:
        return ActivationGateResult(
            ActivationGate.CONFIG_FLAG, False, "config_flag_value_unknown"
        )
    if ctx.config_flag_value is False:
        # Promotion is being requested but the operator-controlled flag
        # is still off — that's actually the correct *initial* state
        # before the gate is flipped, but it means "not yet activated".
        return ActivationGateResult(
            ActivationGate.CONFIG_FLAG,
            False,
            "config_flag_not_enabled",
            details={"path": ctx.config_flag_path},
        )
    return ActivationGateResult(
        ActivationGate.CONFIG_FLAG,
        True,
        "config_flag_enabled",
        details={"path": ctx.config_flag_path},
    )


# ── aggregate ──────────────────────────────────────────────────────────────


@dataclass
class ActivationGates:
    """Composable gate collection. Operator can swap out gates for tests."""

    gates: list = field(
        default_factory=lambda: [
            _gate_feature_contract,
            _gate_model_registered,
            _gate_validation_report,
            _gate_paper_soak,
            _gate_risk_rejection,
            _gate_execution_cost,
            _gate_rollback,
            _gate_config_flag,
        ]
    )

    def evaluate(self, ctx: ActivationContext) -> ActivationVerdict:
        results: list[ActivationGateResult] = []
        for gate_fn in self.gates:
            try:
                results.append(gate_fn(ctx))
            except Exception as exc:  # noqa: BLE001
                results.append(
                    ActivationGateResult(
                        gate=ActivationGate.FEATURE_CONTRACT,  # placeholder
                        passed=False,
                        reason=f"gate_exception:{exc.__class__.__name__}",
                        details={"error": str(exc)},
                    )
                )
        cleared = all(r.passed for r in results)
        return ActivationVerdict(
            model_name=ctx.model_name,
            model_version=ctx.model_version,
            target_status=ctx.target_status,
            gates=results,
            cleared_for_activation=cleared,
        )


# ── module-level convenience ──────────────────────────────────────────────


def evaluate_activation(ctx: ActivationContext) -> ActivationVerdict:
    """Run the default gate suite."""
    return ActivationGates().evaluate(ctx)
