from __future__ import annotations

from decimal import Decimal

from core.models_runtime import ExecutionInstruction
from execution.d015_instruction_executor import risk_signal_from_execution_instruction


def test_d015_instruction_uses_notional_divided_by_price() -> None:
    instr = ExecutionInstruction(
        symbol="BTC-USD",
        action="open",
        side="long",
        target_notional=Decimal("5000"),
        metadata={"strategy_name": "volume_flow"},
    )

    sig = risk_signal_from_execution_instruction(
        instr,
        signal_id="s1",
        broker="binance",
        asset_class="crypto",
        price=Decimal("100000"),
    )

    assert sig.suggested_quantity == Decimal("0.05")
    assert sig.suggested_price == Decimal("100000")
    assert sig.metadata["target_notional"] == "5000"
    assert sig.metadata["risk_notional_override"] == "5000"
    assert sig.strategy == "volume_flow"


def test_d015_instruction_missing_price_does_not_fabricate_quantity() -> None:
    instr = ExecutionInstruction(
        symbol="ETH-USD",
        action="open",
        side="long",
        target_notional=Decimal("6000"),
    )

    sig = risk_signal_from_execution_instruction(
        instr,
        signal_id="s2",
        broker="binance",
        asset_class="crypto",
        price=Decimal("0"),
    )

    assert sig.suggested_quantity == Decimal("0")
    assert sig.suggested_price is None
    assert "risk_notional_override" not in sig.metadata
