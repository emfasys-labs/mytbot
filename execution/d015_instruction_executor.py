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
    px = price if price > 0 else Decimal("1")
    qty = instruction.target_notional / px
    if instruction.action in ("close", "reduce"):
        side = "sell" if instruction.side == "long" else "buy"
    else:
        side = "buy" if instruction.side == "long" else "sell"
    return Signal(
        signal_id=signal_id,
        symbol=instruction.symbol,
        side=side,
        strategy=strategy,
        confidence=0.85,
        suggested_quantity=qty,
        suggested_price=px,
        broker=broker,
        asset_class=asset_class,
        timestamp=datetime.now(timezone.utc).isoformat(),
        metadata={
            "d015_executor": True,
            "instruction_action": instruction.action,
            "instruction_reason": instruction.reason or "",
        },
    )
