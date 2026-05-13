"""
execution/scheduler.py
========================
Wave 9 — Almgren-Chriss-lite urgency policy.

Decides *how* to fill an approved order given the expected execution
cost, the signal urgency, and the regime. Output is one of:

    MARKET        — take liquidity now
    LIMIT         — post at top-of-book (or near), accept partial fills
    PASSIVE       — post inside the book; willing to wait
    SLICED        — break into children (use ``execution.order_slicer``)
    DO_NOT_TRADE  — expected cost exceeds the signal's edge

Pure decision logic — no broker calls, no DB. The router reads the
returned ``UrgencyDecision`` and constructs the actual order.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class Urgency(Enum):
    MARKET = "market"
    LIMIT = "limit"
    PASSIVE = "passive"
    SLICED = "sliced"
    DO_NOT_TRADE = "do_not_trade"


@dataclass
class UrgencyDecision:
    urgency: Urgency
    reason: str
    expected_cost_bps: float
    edge_bps: float = 0.0
    metadata: dict = field(default_factory=dict)


@dataclass
class UrgencyPolicy:
    """
    Configurable thresholds. Defaults are conservative — the operator
    tunes via ``config/execution_models.yaml``.
    """

    # Thresholds in bps.
    market_cost_ceiling: float = 8.0       # below this ⇒ MARKET is fine
    limit_cost_ceiling: float = 25.0       # between market and limit ⇒ LIMIT
    passive_cost_ceiling: float = 60.0     # between limit and passive ⇒ PASSIVE
    # Anything above passive_cost_ceiling triggers SLICED. If even after
    # slicing the per-child cost can't drop below ``do_not_trade_ceiling``,
    # we refuse the trade.
    do_not_trade_ceiling: float = 150.0
    # Required edge/cost cushion. When positive, edge must be at least
    # ``edge_to_cost_safety * expected_cost``; set 0 to disable this veto.
    edge_to_cost_safety: float = 1.0
    # When signal urgency is high (close to 1.0) we accept higher costs;
    # this multiplies the effective ceilings.
    high_urgency_multiplier: float = 1.5
    high_urgency_threshold: float = 0.8


def decide_urgency(
    *,
    expected_cost_bps: float,
    edge_bps: float = 0.0,
    signal_urgency: float = 0.5,
    demand_alignment: float = 0.0,
    regime_label: Optional[str] = None,
    policy: Optional[UrgencyPolicy] = None,
) -> UrgencyDecision:
    """
    Map (cost, edge, urgency, regime) → ``Urgency``.

    Inputs:
      - ``expected_cost_bps``: from ``execution.impact.total_execution_cost_bps``
      - ``edge_bps``: signed expected return × 10_000 (positive = good).
        Pass 0 if not available; the policy degrades gracefully.
      - ``signal_urgency``: 0..1 from ``Opportunity.urgency_score``.
      - ``demand_alignment``: -1..1; positive ⇒ liquidity is on our side.
      - ``regime_label``: optional; "crash" / "panic" forces LIMIT or
        DO_NOT_TRADE to avoid taking liquidity in a vacuum.
    """
    p = policy or UrgencyPolicy()
    cost = max(0.0, float(expected_cost_bps))
    edge = float(edge_bps or 0.0)
    urgency = max(0.0, min(1.0, float(signal_urgency or 0.5)))

    # Stress-regime override: never market into a crash.
    if regime_label and regime_label.lower() in {"crash", "panic"}:
        if cost > p.limit_cost_ceiling * p.high_urgency_multiplier:
            return UrgencyDecision(
                urgency=Urgency.DO_NOT_TRADE,
                reason="stress_regime_high_cost",
                expected_cost_bps=cost,
                edge_bps=edge,
            )
        return UrgencyDecision(
            urgency=Urgency.LIMIT,
            reason="stress_regime_avoid_market",
            expected_cost_bps=cost,
            edge_bps=edge,
        )

    # Edge sanity: refuse trades that do not clear the required edge/cost
    # cushion. The previous form let higher safety values make the gate
    # looser; for profitability this needs to be a minimum edge multiple.
    safety = max(0.0, float(p.edge_to_cost_safety or 0.0))
    if edge > 0 and safety > 0 and edge < cost * safety:
        return UrgencyDecision(
            urgency=Urgency.DO_NOT_TRADE,
            reason="cost_exceeds_edge",
            expected_cost_bps=cost,
            edge_bps=edge,
            metadata={"edge_to_cost_safety": safety},
        )

    # Effective ceilings — relax when signal urgency is high or demand
    # is aligned with our side.
    mult = 1.0
    if urgency >= p.high_urgency_threshold:
        mult *= p.high_urgency_multiplier
    if demand_alignment > 0.5:
        mult *= 1.10  # mild relaxation when liquidity is on our side

    market_cap = p.market_cost_ceiling * mult
    limit_cap = p.limit_cost_ceiling * mult
    passive_cap = p.passive_cost_ceiling * mult
    dnt_cap = p.do_not_trade_ceiling * mult

    if cost <= market_cap:
        return UrgencyDecision(
            urgency=Urgency.MARKET,
            reason="cost_below_market_ceiling",
            expected_cost_bps=cost,
            edge_bps=edge,
            metadata={"effective_market_cap": market_cap},
        )
    if cost <= limit_cap:
        return UrgencyDecision(
            urgency=Urgency.LIMIT,
            reason="market_too_costly_use_limit",
            expected_cost_bps=cost,
            edge_bps=edge,
            metadata={"effective_limit_cap": limit_cap},
        )
    if cost <= passive_cap:
        return UrgencyDecision(
            urgency=Urgency.PASSIVE,
            reason="limit_too_costly_use_passive",
            expected_cost_bps=cost,
            edge_bps=edge,
            metadata={"effective_passive_cap": passive_cap},
        )
    if cost <= dnt_cap:
        return UrgencyDecision(
            urgency=Urgency.SLICED,
            reason="single_shot_too_costly_slice",
            expected_cost_bps=cost,
            edge_bps=edge,
            metadata={"effective_dnt_cap": dnt_cap},
        )
    return UrgencyDecision(
        urgency=Urgency.DO_NOT_TRADE,
        reason="cost_above_do_not_trade_ceiling",
        expected_cost_bps=cost,
        edge_bps=edge,
        metadata={"effective_dnt_cap": dnt_cap},
    )
