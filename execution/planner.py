"""
execution/planner.py
====================
D015: translate ``AllocationDecision`` into ``ExecutionPlan`` instructions.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal

from config.loaders import load_allocation
from config.models import AllocationConfig
from core.models_runtime import AllocationDecision, ExecutionInstruction, ExecutionPlan, PortfolioState, clip_decimal

logger = logging.getLogger(__name__)


def _pos_map(portfolio_state: PortfolioState) -> dict[str, Decimal]:
    return {p.symbol: p.market_value for p in portfolio_state.positions}


def build_execution_plan(
    *,
    decision: AllocationDecision,
    portfolio_state: PortfolioState,
    allocation_cfg: AllocationConfig | None = None,
    now: datetime | None = None,
) -> ExecutionPlan:
    cfg = allocation_cfg or load_allocation()
    ts = now or datetime.now(timezone.utc)
    instructions: list[ExecutionInstruction] = []
    pm = _pos_map(portfolio_state)
    slip_cap = Decimal(str(cfg.safety.emergency_bounds.max_execution_slippage_pct.get(decision.mode, 0.007)))
    urg = cfg.execution_plan_urgency

    for rc in decision.replacement_candidates:
        instructions.append(
            ExecutionInstruction(
                symbol=rc.old_symbol,
                action="close",
                side="long",
                target_notional=pm.get(rc.old_symbol, Decimal("0")),
                max_slippage_bps=slip_cap * Decimal("10000"),
                urgency_score=Decimal(str(urg.replace_close_old)),
                close_only=True,
                reason=f"replace_with:{rc.new_symbol}",
                metadata={"d015": True},
            )
        )
        tgt = next((t for t in decision.allocation_targets if t.symbol == rc.new_symbol), None)
        if tgt:
            instructions.append(
                ExecutionInstruction(
                    symbol=rc.new_symbol,
                    action="open",
                    side=tgt.side,
                    target_notional=tgt.target_notional,
                    target_weight=tgt.target_weight,
                    max_slippage_bps=slip_cap * Decimal("10000"),
                    urgency_score=Decimal(str(urg.replacement_open)),
                    reason="replacement_open",
                    metadata={"d015": True},
                )
            )

    for sym in decision.close_symbols:
        if any(i.symbol == sym and i.action == "close" for i in instructions):
            continue
        instructions.append(
            ExecutionInstruction(
                symbol=sym,
                action="close",
                side="long",
                target_notional=pm.get(sym, Decimal("0")),
                max_slippage_bps=slip_cap * Decimal("10000"),
                urgency_score=Decimal(str(urg.allocation_close)),
                close_only=True,
                reason="allocation_close",
                metadata={"d015": True},
            )
        )

    for t in decision.allocation_targets:
        cur = pm.get(t.symbol, Decimal("0"))
        delta = t.target_notional - cur
        if delta <= Decimal("0"):
            tol = Decimal(str(urg.reduce_vs_target_tolerance))
            if cur > t.target_notional * tol and t.target_notional > 0:
                instructions.append(
                    ExecutionInstruction(
                        symbol=t.symbol,
                        action="reduce",
                        side=t.side,
                        target_notional=clip_decimal(cur - t.target_notional, Decimal("0"), cur),
                        max_slippage_bps=slip_cap * Decimal("10000"),
                        urgency_score=Decimal(str(urg.allocation_reduce)),
                        reduce_only=True,
                        reason="allocation_reduce",
                        metadata={"d015": True},
                    )
                )
            continue
        if t.symbol in decision.open_symbols or cur == 0:
            if not any(i.symbol == t.symbol and i.action == "open" for i in instructions):
                instructions.append(
                    ExecutionInstruction(
                        symbol=t.symbol,
                        action="increase" if cur > 0 else "open",
                        side=t.side,
                        target_notional=delta,
                        target_weight=t.target_weight,
                        max_slippage_bps=slip_cap * Decimal("10000"),
                        urgency_score=Decimal(str(urg.allocation_open_or_increase)),
                        reason="allocation_open_or_increase",
                        metadata={"d015": True},
                    )
                )

    turnover = sum(i.target_notional for i in instructions)
    return ExecutionPlan(
        timestamp=ts,
        mode=decision.mode,
        instructions=instructions,
        estimated_turnover=turnover,
        estimated_cost_bps=slip_cap * Decimal("10000"),
        rationale=decision.rationale,
        metadata={"d015": True, "instruction_count": len(instructions)},
    )
