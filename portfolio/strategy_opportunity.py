from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any


@dataclass(frozen=True)
class StrategyOpportunity:
    strategy_name: str
    symbol: str
    side: str
    created_at: datetime

    expected_edge: Decimal
    confidence: Decimal
    capital_required: Decimal
    expected_holding_hours: int

    liquidity_score: Decimal
    execution_score: Decimal
    regime_fit_score: Decimal
    risk_cost_score: Decimal

    priority_score: Decimal
    metadata: dict[str, Any] = field(default_factory=dict)


def compute_priority_score(
    expected_edge: Decimal,
    confidence: Decimal,
    regime_fit: Decimal,
    execution_score: Decimal,
    risk_cost: Decimal,
) -> Decimal:
    return (expected_edge * confidence * regime_fit * execution_score) - risk_cost
