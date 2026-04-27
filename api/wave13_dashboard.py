"""
api/wave13_dashboard.py
=========================
Wave 13 — dashboard payload aggregator.

Builds a single structured ``dict`` the operator's UI renders. The
payload distinguishes:

  - "no signal"       — ``generated == 0`` for the strategy
  - "blocked by model"  — meta_label_blocked > 0 OR forecast_blocked > 0
  - "blocked by risk"   — risk_rejected > 0
  - "blocked by execution" — execution_blocked > 0

Every block in the payload is derived from already-stamped metadata
(Wave 2 meta-label, Wave 6 forecast, Wave 8 vol-targeting, Wave 9 cost
gate, etc.) plus the funnel counters from
``system/funnel_telemetry.py``.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional

import yaml

from system.funnel_telemetry import (
    FunnelTelemetry,
    get_default_funnel_telemetry,
)

logger = logging.getLogger(__name__)


# ── helpers ────────────────────────────────────────────────────────────────


def _yaml_or_empty(path: str | Path) -> dict:
    p = Path(path)
    if not p.exists():
        return {}
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return {}
    return raw if isinstance(raw, dict) else {}


def _classify_strategy_status(per_strategy: dict[str, dict[str, int]]) -> dict[str, dict[str, Any]]:
    """For each strategy, derive a single 'status' tag for the UI funnel."""
    out: dict[str, dict[str, Any]] = {}
    for strategy, stages in per_strategy.items():
        evaluated = int(stages.get("evaluated", 0))
        generated = int(stages.get("generated", 0))
        meta_blocked = int(stages.get("meta_label_blocked", 0))
        fcst_blocked = int(stages.get("forecast_blocked", 0))
        risk_rej = int(stages.get("risk_rejected", 0))
        exec_blocked = int(stages.get("execution_blocked", 0))
        executed = int(stages.get("executed", 0))

        if executed > 0:
            status = "trading"
        elif exec_blocked > 0:
            status = "blocked_by_execution"
        elif risk_rej > 0:
            status = "blocked_by_risk"
        elif meta_blocked > 0 or fcst_blocked > 0:
            status = "blocked_by_model"
        elif generated == 0 and evaluated == 0:
            status = "idle"
        elif generated == 0:
            status = "no_signal"
        else:
            status = "in_flight"

        out[strategy] = {
            "status": status,
            "evaluated": evaluated,
            "generated": generated,
            "executed": executed,
            "model_blocks": meta_blocked + fcst_blocked,
            "risk_rejections": risk_rej,
            "execution_blocks": exec_blocked,
        }
    return out


# ── strategy-coverage source ─────────────────────────────────────────────


def _collect_strategy_coverage(
    *,
    strategies_yaml_path: str = "config/strategies.yaml",
    options_yaml_path: str = "config/options_strategies.yaml",
    factor_yaml_path: str = "config/factor_sleeve.yaml",
    pairs_yaml_path: str = "config/pairs_trading.yaml",
) -> dict[str, Any]:
    """Pull on/off state for the strategy families landed in waves 2-12."""
    out: dict[str, Any] = {"families": []}

    # Wave 2: trained meta-labeller.
    raw_strat = _yaml_or_empty(strategies_yaml_path)
    sig_engine = raw_strat.get("signal_engine") or {}
    out["families"].append({
        "name": "trained_meta_labeler",
        "wave": 2,
        "enabled": bool(sig_engine.get("use_trained_meta_labeler", False)),
        "config_path": strategies_yaml_path,
    })

    # Wave 3: factor sleeve.
    raw_factor = _yaml_or_empty(factor_yaml_path)
    fs = raw_factor.get("factor_sleeve") or {}
    out["families"].append({
        "name": "factor_sleeve",
        "wave": 3,
        "enabled": bool(fs.get("enabled", False)),
        "config_path": factor_yaml_path,
    })

    # Wave 5: stat-arb pairs.
    raw_pairs = _yaml_or_empty(pairs_yaml_path)
    sap = raw_pairs.get("stat_arb_pairs") or {}
    out["families"].append({
        "name": "stat_arb_pairs",
        "wave": 5,
        "enabled": bool(sap.get("enabled", False)),
        "config_path": pairs_yaml_path,
    })

    # Wave 12: options.
    raw_opt = _yaml_or_empty(options_yaml_path)
    od = raw_opt.get("options_directional") or {}
    oh = raw_opt.get("options_hedging") or {}
    out["families"].append({
        "name": "options_directional",
        "wave": 12,
        "enabled": bool(od.get("enabled", False)),
        "paper_only": bool(od.get("paper_only", True)),
        "config_path": options_yaml_path,
    })
    out["families"].append({
        "name": "options_hedging",
        "wave": 12,
        "enabled": bool(oh.get("enabled", False)),
        "paper_only": bool(oh.get("paper_only", True)),
        "config_path": options_yaml_path,
    })

    return out


# ── model-health source ───────────────────────────────────────────────────


def _collect_model_health(
    *,
    registry_yaml_path: str = "config/model_registry.yaml",
    feature_freshness_seconds: Optional[float] = None,
    last_prediction_count: Optional[int] = None,
) -> dict[str, Any]:
    """Read registered models from YAML and surface a per-model row."""
    raw = _yaml_or_empty(registry_yaml_path)
    models_raw = raw.get("models") or []
    rows: list[dict[str, Any]] = []
    for m in models_raw:
        if not isinstance(m, Mapping):
            continue
        rows.append({
            "name": m.get("name"),
            "version": m.get("version"),
            "task": m.get("task"),
            "target": m.get("target"),
            "approval_status": m.get("approval_status", "research"),
            "calibration_method": m.get("calibration_method", "none"),
            "feature_contract_hash": m.get("feature_contract_hash"),
        })
    return {
        "registered_models": rows,
        "registry_path": registry_yaml_path,
        "feature_freshness_seconds": feature_freshness_seconds,
        "last_prediction_count": last_prediction_count,
        "stale_model_warning": bool(
            feature_freshness_seconds is not None and feature_freshness_seconds > 60.0
        ),
    }


# ── portfolio + execution from snapshot ──────────────────────────────────


def _collect_portfolio_intelligence(snapshot: Optional[Mapping[str, Any]]) -> dict[str, Any]:
    if not snapshot:
        return {"available": False}
    md = snapshot.get("metadata") or {}
    out = {
        "available": True,
        "gross_exposure_target": snapshot.get("gross_exposure_target"),
        "net_exposure_target": snapshot.get("net_exposure_target"),
        "capital_deployment_target": snapshot.get("capital_deployment_target"),
        "wave8_vol_overlay_used": bool(md.get("wave8_vol_overlay_used", False)),
        "wave8_vol_overlay_scale": md.get("wave8_vol_overlay_scale"),
        "wave8_vol_overlay_drawdown": md.get("wave8_vol_overlay_drawdown"),
        "wave8_vol_overlay_realised_vol": md.get("wave8_vol_overlay_realised_vol"),
        "demand_score": md.get("demand_score"),
        "demand_trend": md.get("demand_trend"),
    }
    targets = snapshot.get("allocation_targets") or []
    out["target_weights"] = [
        {
            "symbol": t.get("symbol"),
            "target_weight": t.get("target_weight"),
            "target_notional": t.get("target_notional"),
            "side": t.get("side"),
            "strategy": t.get("strategy_name"),
        }
        for t in targets
    ]
    return out


def _collect_execution_intelligence(*, gate_blocked: int = 0, gate_passed: int = 0) -> dict[str, Any]:
    return {
        "wave9_gate_passed": int(gate_passed),
        "wave9_gate_blocked": int(gate_blocked),
        "execution_block_rate": (
            float(gate_blocked) / float(gate_blocked + gate_passed)
            if (gate_blocked + gate_passed) > 0
            else 0.0
        ),
    }


# ── public API ────────────────────────────────────────────────────────────


def build_wave13_payload(
    *,
    funnel: Optional[FunnelTelemetry] = None,
    snapshot: Optional[Mapping[str, Any]] = None,
    feature_freshness_seconds: Optional[float] = None,
    last_prediction_count: Optional[int] = None,
    wave9_gate_passed: int = 0,
    wave9_gate_blocked: int = 0,
    strategies_yaml_path: str = "config/strategies.yaml",
    options_yaml_path: str = "config/options_strategies.yaml",
    factor_yaml_path: str = "config/factor_sleeve.yaml",
    pairs_yaml_path: str = "config/pairs_trading.yaml",
    registry_yaml_path: str = "config/model_registry.yaml",
) -> dict[str, Any]:
    """
    Build the Wave 13 dashboard payload.

    All inputs are optional — missing pieces map to ``available=False``
    blocks so the UI renders an empty card rather than crashing.
    """
    f = funnel or get_default_funnel_telemetry()
    snap = f.snapshot()
    funnel_dict = snap.to_dict()

    strategy_status = _classify_strategy_status(funnel_dict.get("per_strategy") or {})

    return {
        "schema": "wave13_dashboard",
        "version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "funnel": funnel_dict,
        "strategy_status": strategy_status,
        "strategy_coverage": _collect_strategy_coverage(
            strategies_yaml_path=strategies_yaml_path,
            options_yaml_path=options_yaml_path,
            factor_yaml_path=factor_yaml_path,
            pairs_yaml_path=pairs_yaml_path,
        ),
        "model_health": _collect_model_health(
            registry_yaml_path=registry_yaml_path,
            feature_freshness_seconds=feature_freshness_seconds,
            last_prediction_count=last_prediction_count,
        ),
        "portfolio_intelligence": _collect_portfolio_intelligence(snapshot),
        "execution_intelligence": _collect_execution_intelligence(
            gate_blocked=wave9_gate_blocked,
            gate_passed=wave9_gate_passed,
        ),
    }
