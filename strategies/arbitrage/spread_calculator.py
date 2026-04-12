from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

BPS = Decimal("10000")


def quantize_spread(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.00000001"), rounding=ROUND_HALF_UP)


def compute_spread_bps(bid: Decimal, ask: Decimal) -> Decimal:
    """Edge in bps when buying at ``ask`` on venue A and selling at ``bid`` on venue B."""
    if ask <= 0:
        return Decimal("0")
    return ((bid - ask) / ask) * BPS


def compute_gross_spread(notional: Decimal, bid: Decimal, ask: Decimal) -> Decimal:
    if ask <= 0:
        return Decimal("0")
    return quantize_spread(notional * (bid - ask) / ask)


def compute_net_spread(
    notional: Decimal,
    bid: Decimal,
    ask: Decimal,
    fee_bps: Decimal,
    slippage_bps: Decimal,
) -> Decimal:
    gross = notional * (bid - ask) / ask if ask > 0 else Decimal("0")
    total_cost = notional * (fee_bps + slippage_bps) / BPS
    return quantize_spread(gross - total_cost)
