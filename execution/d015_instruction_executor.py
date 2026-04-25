"""Map D015 ``ExecutionInstruction`` rows to ``risk.engine.Signal`` for the execution engine."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from core.models_runtime import ExecutionInstruction
from risk.engine import Signal


def risk_signal_from_execution_instruction(
    instruction: ExecutionInstruction,
    *,
    signal_id: str,
    broker: str,
    asset_class: str,
    price: Decimal,
    strategy: str = "d015_allocator",
) -> Signal:
    md = instruction.metadata if isinstance(instruction.metadata, dict) else {}
    strat = md.get("strategy_name")
    if isinstance(strat, str) and strat.strip():
        strategy = strat.strip()
    px = price if price > 0 else Decimal("0")
    if instruction.target_quantity is not None and instruction.target_quantity > 0:
        qty = instruction.target_quantity
    elif px > 0:
        qty = instruction.target_notional / px
    else:
        qty = Decimal("0")
    if instruction.action in ("close", "reduce"):
        side = "sell" if instruction.side == "long" else "buy"
    else:
        side = "buy" if instruction.side == "long" else "sell"
    out_md = {
        **md,
        "d015_executor": True,
        "instruction_action": instruction.action,
        "instruction_reason": instruction.reason or "",
        "target_notional": str(instruction.target_notional),
        "target_quantity": str(instruction.target_quantity) if instruction.target_quantity is not None else "",
        "reduce_only": bool(instruction.reduce_only),
        "close_only": bool(instruction.close_only),
    }
    if px > 0 and instruction.target_notional >= 0:
        out_md["risk_notional_override"] = str(instruction.target_notional)

    return Signal(
        signal_id=signal_id,
        symbol=instruction.symbol,
        side=side,
        strategy=strategy,
        confidence=0.85,
        suggested_quantity=qty,
        suggested_price=px if px > 0 else None,
        broker=broker,
        asset_class=asset_class,
        timestamp=datetime.now(timezone.utc).isoformat(),
        metadata=out_md,
    )
