"""Hold score and switching-cost helpers for D015 allocation."""

from __future__ import annotations

from decimal import Decimal

from config.models import AllocationConfig
from core.models_runtime import HeldPositionState, Opportunity, clip_decimal
from execution.router import BROKER_FEE_MAP


def compute_hold_score(
    pos: HeldPositionState,
    opp_by_symbol: dict[str, Opportunity],
    allocation_cfg: AllocationConfig,
) -> Decimal:
    hc = allocation_cfg.hold_score.components
    opp = opp_by_symbol.get(pos.symbol)
    cur = opp.opportunity_score if opp else pos.current_opportunity_score
    nd = allocation_cfg.hold_score.neutral_defaults
    pnl_off = Decimal(str(nd.unrealised_pnl_offset))
    trend_def = Decimal(str(nd.trend_continuation_default))
    pnl_q = clip_decimal(pos.unrealised_pnl_pct + pnl_off, Decimal("0"), Decimal("1"))
    trend = clip_decimal(pos.metadata.get("trend_continuation", trend_def), Decimal("0"), Decimal("1"))  # type: ignore[arg-type]
    exit_p = clip_decimal(pos.exit_pressure, Decimal("0"), Decimal("1"))
    oc = clip_decimal(pos.opportunity_cost, Decimal("0"), Decimal("1"))
    return (
        Decimal(str(hc.current_opportunity_score)) * cur
        + Decimal(str(hc.unrealised_pnl_quality)) * pnl_q
        + Decimal(str(hc.trend_continuation)) * trend
        - Decimal(str(hc.negative_exit_pressure)) * exit_p
        - Decimal(str(hc.negative_opportunity_cost)) * oc
    )


def estimate_fee_bps_for_asset(
    asset_class: str,
    broker_hint: str | None,
    *,
    clip_min: Decimal,
    clip_max: Decimal,
) -> Decimal:
    b = (broker_hint or "ibkr").lower()
    fee = BROKER_FEE_MAP.get(b, Decimal("0.002"))
    return clip_decimal(fee * Decimal("10000"), clip_min, clip_max)


def compute_switching_cost_score(
    *,
    opportunity: Opportunity,
    position: HeldPositionState,
    allocation_cfg: AllocationConfig,
    round_trip: bool = True,
) -> Decimal:
    """
    Map fees+spread+slippage to a 0..1 penalty (higher = more expensive to switch).
    """
    sc = allocation_cfg.replacement_logic.switching_cost
    norm = allocation_cfg.replacement_logic.switching_cost_normalisation
    clip_hi = Decimal(str(norm.spread_slippage_bps_clip_max))
    spread_bps = clip_decimal(opportunity.spread_bps, Decimal("0"), clip_hi)
    slip_bps = clip_decimal(opportunity.slippage_bps_estimate, Decimal("0"), clip_hi)
    fee_bps = estimate_fee_bps_for_asset(
        position.asset_class,
        position.metadata.get("broker"),  # type: ignore[arg-type]
        clip_min=Decimal(str(norm.fee_bps_clip_min)),
        clip_max=Decimal(str(norm.fee_bps_clip_max)),
    )
    urgency = clip_decimal(opportunity.urgency_score, Decimal("0"), Decimal("1"))
    raw_bps = (
        Decimal(str(sc.fee_weight)) * fee_bps * (Decimal("2") if round_trip else Decimal("1"))
        + Decimal(str(sc.spread_weight)) * spread_bps
        + Decimal(str(sc.slippage_weight)) * slip_bps
    )
    u_scale = Decimal(str(norm.urgency_discount_bps_scale))
    discount = Decimal(str(sc.urgency_discount_weight)) * urgency * u_scale
    net_max = Decimal(str(norm.net_bps_max))
    net_bps = clip_decimal(raw_bps - discount, Decimal("0"), net_max)
    div = Decimal(str(norm.penalty_divisor_bps))
    return clip_decimal(net_bps / div, Decimal("0"), Decimal("1"))
