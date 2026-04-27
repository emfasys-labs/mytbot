"""
execution/impact.py
=====================
Wave 9 — market impact model.

Square-root impact (Almgren / Kissell):

    impact_bps = c * daily_volatility * sqrt(participation_rate) * 10_000

where ``participation_rate = order_size / daily_volume``. The
coefficient ``c`` is asset-class-dependent and is the operator's
calibration target (defaults are conservative starting points).

The total cost helper combines:

    total_cost_bps = fee_bps + spread_bps + slippage_bps + impact_bps

so the router can rank brokers / venues on a single bps number that's
comparable across assets and order sizes.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional


# Conservative default coefficients per asset class. These are tuneable
# via ``config/execution_models.yaml`` once the operator collects fill
# telemetry. Sources: Almgren-Chriss empirical estimates for liquid
# US equities; halved for crypto due to fragmentation, doubled for
# small-cap / FX-exotic.
DEFAULT_IMPACT_COEFFICIENTS: dict[str, float] = {
    "equity": 0.10,
    "etf": 0.08,
    "bond": 0.05,
    "forex": 0.06,
    "crypto": 0.05,
    "future": 0.10,
    "option": 0.20,
    "other": 0.10,
}


@dataclass
class ImpactInputs:
    order_qty: float
    daily_volume: float
    daily_volatility: float  # annualised decimal (e.g. 0.20 for 20%)
    asset_class: str = "equity"


def participation_rate(order_qty: float, daily_volume: float) -> float:
    """``order_qty / daily_volume`` clipped to [0, ∞). Returns 0 on bad input."""
    if daily_volume is None or daily_volume <= 0 or not math.isfinite(daily_volume):
        return 0.0
    if order_qty is None or not math.isfinite(order_qty):
        return 0.0
    return abs(float(order_qty)) / float(daily_volume)


def square_root_impact_bps(
    *,
    order_qty: float,
    daily_volume: float,
    daily_volatility: float,
    asset_class: str = "equity",
    coefficient: Optional[float] = None,
) -> float:
    """
    Square-root impact in basis points.

    Returns ``0.0`` on degenerate inputs (zero volume, non-finite vol).
    Caller should treat zero impact as "not enough data to estimate" —
    the scheduler will degrade to its conservative default urgency.
    """
    pr = participation_rate(order_qty, daily_volume)
    if pr <= 0:
        return 0.0
    if daily_volatility is None or daily_volatility <= 0 or not math.isfinite(daily_volatility):
        return 0.0
    c = (
        float(coefficient)
        if coefficient is not None
        else DEFAULT_IMPACT_COEFFICIENTS.get(asset_class, DEFAULT_IMPACT_COEFFICIENTS["other"])
    )
    return float(c * float(daily_volatility) * math.sqrt(pr) * 10_000.0)


@dataclass
class CostBreakdown:
    fee_bps: float
    spread_bps: float
    slippage_bps: float
    impact_bps: float

    @property
    def total_bps(self) -> float:
        return float(self.fee_bps + self.spread_bps + self.slippage_bps + self.impact_bps)


def total_execution_cost_bps(
    *,
    order_qty: float,
    daily_volume: float,
    daily_volatility: float,
    asset_class: str = "equity",
    fee_bps: float = 0.0,
    spread_bps: float = 0.0,
    slippage_bps: float = 0.0,
    coefficient: Optional[float] = None,
) -> CostBreakdown:
    """All-in expected execution cost in bps with per-component breakdown."""
    impact = square_root_impact_bps(
        order_qty=order_qty,
        daily_volume=daily_volume,
        daily_volatility=daily_volatility,
        asset_class=asset_class,
        coefficient=coefficient,
    )
    return CostBreakdown(
        fee_bps=float(fee_bps or 0.0),
        spread_bps=float(spread_bps or 0.0),
        slippage_bps=float(slippage_bps or 0.0),
        impact_bps=float(impact),
    )
