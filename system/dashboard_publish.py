"""
Persist dashboard-facing decision snapshots to ``ControlState`` (``CommandBus.set_state``).

The API and WebSocket layer read ``DASHBOARD_SNAPSHOT_KEY`` so the UI can show allocator
intent without peeking at in-process trading state.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Literal

from control.command_bus import CommandBus
from core.models_runtime import (
    AllocationDecision,
    AllocationTarget,
    ExecutionPlan,
    Opportunity,
    PortfolioState,
    RegimeState,
    ReplacementCandidate,
)
from portfolio.strategy_opportunity import StrategyOpportunity
from signals.accumulator import SignalAccumulator

DASHBOARD_SNAPSHOT_KEY = "dashboard.snapshot"

PathKind = Literal["d015", "global_edge"]


def _d(x: Decimal | None) -> str:
    if x is None:
        return "0"
    return str(x)


def serialize_opportunity(o: Opportunity, *, rank: int = 0) -> dict[str, Any]:
    comp = o.components.as_dict()
    vol = o.volume_flow
    out: dict[str, Any] = {
        "symbol": o.symbol,
        "asset_class": o.asset_class,
        "side": o.side,
        "opportunity_score": _d(o.opportunity_score),
        "urgency_score": _d(o.urgency_score),
        "confidence": _d(o.confidence),
        "tags": list(o.tags)[:16],
        "components": {k: _d(v) for k, v in comp.items()},
        "rank": rank,
    }
    if vol is not None:
        f = vol.features
        out["volume_anomaly"] = {
            "detection_strength": _d(vol.detection_strength),
            "volume_z": _d(f.volume_z),
            "relative_dollar_volume": _d(f.relative_dollar_volume),
            "fake_spike_penalty": _d(f.fake_spike_penalty),
            "metadata": {k: str(v)[:120] for k, v in list((vol.metadata or {}).items())[:12]},
        }
    return out


def serialize_regime_state(r: RegimeState) -> dict[str, Any]:
    mc = r.components
    return {
        "timestamp": r.timestamp.isoformat(),
        "regime_label": r.regime_label,
        "market_state_score": _d(r.market_state_score),
        "drawdown_throttle": _d(r.drawdown_throttle),
        "execution_quality": _d(r.execution_quality),
        "breadth_score": _d(r.breadth_score),
        "components": {k: _d(v) for k, v in mc.as_dict().items()},
        "metadata": {k: v for k, v in (r.metadata or {}).items() if isinstance(v, (str, int, float, bool))},
    }


def serialize_replacement_candidate(c: ReplacementCandidate) -> dict[str, Any]:
    return {
        "new_symbol": c.new_symbol,
        "old_symbol": c.old_symbol,
        "new_opportunity_score": _d(c.new_opportunity_score),
        "old_hold_score": _d(c.old_hold_score),
        "switching_cost_score": _d(c.switching_cost_score),
        "replacement_advantage": _d(c.replacement_advantage),
        "recommended_action": c.recommended_action,
        "reason": (c.reason or "")[:500],
    }


def _serialize_allocation_target(t: AllocationTarget) -> dict[str, Any]:
    return {
        "symbol": t.symbol,
        "target_weight": _d(t.target_weight),
        "target_notional": _d(t.target_notional),
        "target_leverage": _d(t.target_leverage),
        "side": t.side,
        "source_opportunity_score": _d(t.source_opportunity_score),
        "priority_rank": int(t.priority_rank),
        "strategy_name": t.strategy_name,
    }


def serialize_allocation_decision(d: AllocationDecision) -> dict[str, Any]:
    return {
        "timestamp": d.timestamp.isoformat(),
        "mode": d.mode,
        "gross_exposure_target": _d(d.gross_exposure_target),
        "net_exposure_target": _d(d.net_exposure_target),
        "capital_deployment_target": _d(d.capital_deployment_target),
        "allocation_targets": [_serialize_allocation_target(t) for t in d.allocation_targets[:24]],
        "open_symbols": list(d.open_symbols)[:32],
        "close_symbols": list(d.close_symbols)[:32],
        "increase_symbols": list(d.increase_symbols)[:32],
        "reduce_symbols": list(d.reduce_symbols)[:32],
        "hold_symbols": list(d.hold_symbols)[:48],
        "replacement_candidates": [serialize_replacement_candidate(x) for x in d.replacement_candidates[:12]],
        "rationale": (d.rationale or "")[:800],
        "metadata": {k: str(v)[:200] for k, v in list((d.metadata or {}).items())[:24]},
    }


def serialize_execution_plan(p: ExecutionPlan) -> dict[str, Any]:
    instrs: list[dict[str, Any]] = []
    for ins in p.instructions[:25]:
        instrs.append(
            {
                "symbol": ins.symbol,
                "action": ins.action,
                "side": ins.side,
                "target_notional": _d(ins.target_notional),
                "target_quantity": _d(ins.target_quantity) if ins.target_quantity is not None else None,
                "target_weight": _d(ins.target_weight) if ins.target_weight is not None else None,
                "urgency_score": _d(ins.urgency_score),
                "reduce_only": bool(ins.reduce_only),
                "close_only": bool(ins.close_only),
                "reason": (ins.reason or "")[:400],
            }
        )
    return {
        "timestamp": p.timestamp.isoformat(),
        "mode": p.mode,
        "instructions": instrs,
        "estimated_turnover": _d(p.estimated_turnover),
        "estimated_cost_bps": _d(p.estimated_cost_bps),
        "rationale": (p.rationale or "")[:800],
    }


def serialize_held_positions(portfolio: PortfolioState, *, limit: int = 12) -> dict[str, Any]:
    positions = list(portfolio.positions)
    by_hold = sorted(positions, key=lambda h: h.hold_score)
    weakest = by_hold[:limit]
    by_exit = sorted(positions, key=lambda h: h.exit_pressure, reverse=True)
    pressured = by_exit[:limit]
    rows: list[dict[str, Any]] = []
    for h in positions[:limit]:
        rows.append(
            {
                "symbol": h.symbol,
                "asset_class": h.asset_class,
                "side": h.side,
                "market_value": _d(h.market_value),
                "hold_score": _d(h.hold_score),
                "exit_pressure": _d(h.exit_pressure),
                "opportunity_cost": _d(h.opportunity_cost),
                "current_opportunity_score": _d(h.current_opportunity_score),
                "unrealised_pnl": _d(h.unrealised_pnl),
                "tags": list(h.tags)[:8],
            }
        )
    return {
        "nav": _d(portfolio.nav),
        "gross_exposure": _d(portfolio.gross_exposure),
        "net_exposure": _d(portfolio.net_exposure),
        "drawdown_from_hwm_pct": _d(portfolio.drawdown_from_hwm_pct),
        "positions_sample": rows,
        "weakest_by_hold_score": [
            {
                "symbol": h.symbol,
                "hold_score": _d(h.hold_score),
                "exit_pressure": _d(h.exit_pressure),
            }
            for h in weakest
        ],
        "highest_exit_pressure": [
            {
                "symbol": h.symbol,
                "hold_score": _d(h.hold_score),
                "exit_pressure": _d(h.exit_pressure),
            }
            for h in pressured
        ],
    }


def serialize_strategy_opportunity(o: StrategyOpportunity) -> dict[str, Any]:
    ps = _d(o.priority_score)
    return {
        "strategy_name": o.strategy_name,
        "symbol": o.symbol,
        "side": o.side,
        "expected_edge": _d(o.expected_edge),
        "confidence": _d(o.confidence),
        "capital_required": _d(o.capital_required),
        "priority_score": ps,
        # Dashboard parity with D015 opportunity rows (UI ranks on opportunity_score).
        "opportunity_score": ps,
        "tags": [o.strategy_name] if o.strategy_name else [],
        "created_at": o.created_at.isoformat() if o.created_at else None,
    }


def serialize_held_edges(edges: list[Any], *, limit: int = 12) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for e in edges[:limit]:
        out.append(
            {
                "symbol": getattr(e, "symbol", ""),
                "notional": _d(getattr(e, "notional", None)),
                "expected_remaining_edge": _d(getattr(e, "expected_remaining_edge", None)),
                "strategy_name": getattr(e, "strategy_name", ""),
                "broker": getattr(e, "broker", ""),
            }
        )
    return out


def serialize_coordinator_actions(actions: list[Any], *, limit: int = 20) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for a in actions[:limit]:
        kind = getattr(a, "kind", "")
        out.append(
            {
                "kind": kind,
                "action": kind,
                "symbol": getattr(a, "symbol", ""),
                "strategy_name": getattr(a, "strategy_name", ""),
                "capital": _d(getattr(a, "capital", None)),
                "priority_score": _d(getattr(a, "priority_score", None)),
            }
        )
    return out


def snapshot_fingerprint(payload: dict[str, Any]) -> str:
    """Stable short hash for WebSocket change detection."""
    try:
        blob = json.dumps(payload, sort_keys=True, default=str)
    except (TypeError, ValueError):
        blob = str(payload)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


async def publish_dashboard_snapshot_d015(
    bus: CommandBus,
    *,
    path: PathKind,
    loop_iteration: int,
    accumulator: SignalAccumulator | None,
    regime: RegimeState | None,
    opportunities: list[Opportunity],
    decision: AllocationDecision,
    plan: ExecutionPlan,
    portfolio_state: PortfolioState,
    max_opps: int = 15,
) -> None:
    ranked = sorted(opportunities, key=lambda o: o.opportunity_score, reverse=True)[:max_opps]
    acc_blob: dict[str, Any] | None = None
    if accumulator is not None:
        acc_blob = accumulator.dashboard_snapshot(top_n=10)

    payload: dict[str, Any] = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "path": path,
        "loop_iteration": int(loop_iteration),
        "accumulator": acc_blob,
        "regime": serialize_regime_state(regime) if regime is not None else None,
        "opportunities": [serialize_opportunity(o, rank=i + 1) for i, o in enumerate(ranked)],
        "allocation": serialize_allocation_decision(decision),
        "execution_plan": serialize_execution_plan(plan),
        "portfolio": serialize_held_positions(portfolio_state),
    }
    payload["fingerprint"] = snapshot_fingerprint(payload)
    await bus.set_state(DASHBOARD_SNAPSHOT_KEY, payload)


async def publish_dashboard_snapshot_global_edge(
    bus: CommandBus,
    *,
    loop_iteration: int,
    accumulator: SignalAccumulator | None,
    held: list[Any],
    strategy_opportunities: list[StrategyOpportunity],
    coordinator_actions: list[Any],
    portfolio_state: PortfolioState,
    max_opps: int = 15,
) -> None:
    acc_blob: dict[str, Any] | None = None
    if accumulator is not None:
        acc_blob = accumulator.dashboard_snapshot(top_n=10)

    ranked = sorted(strategy_opportunities, key=lambda o: o.priority_score, reverse=True)[:max_opps]
    payload: dict[str, Any] = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "path": "global_edge",
        "loop_iteration": int(loop_iteration),
        "accumulator": acc_blob,
        "regime": None,
        "opportunities": [serialize_strategy_opportunity(o) for o in ranked],
        "allocation": None,
        "execution_plan": {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "mode": str(portfolio_state.mode),
            "instructions": serialize_coordinator_actions(coordinator_actions, limit=25),
            "estimated_turnover": "0",
            "estimated_cost_bps": "0",
            "rationale": "global_edge_coordinator",
        },
        "portfolio": serialize_held_positions(portfolio_state),
        "global_edge": {
            "held_edges": serialize_held_edges(held, limit=12),
            "ranked_new_edges": [serialize_strategy_opportunity(o) for o in ranked],
        },
    }
    payload["fingerprint"] = snapshot_fingerprint(payload)
    await bus.set_state(DASHBOARD_SNAPSHOT_KEY, payload)
