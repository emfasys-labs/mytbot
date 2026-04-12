from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

BPS_DENOMINATOR = Decimal("10000")
HOURS_PER_YEAR = Decimal("8760")


def quantize_money(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.00000001"), rounding=ROUND_HALF_UP)


def compute_basis_bps(perp_mark: Decimal, spot_mid: Decimal) -> Decimal:
    if spot_mid <= 0:
        return Decimal("0")
    return ((perp_mark - spot_mid) / spot_mid) * BPS_DENOMINATOR


def compute_gross_funding_per_event(notional: Decimal, funding_rate: Decimal) -> Decimal:
    return quantize_money(notional * funding_rate)


def compute_annualised_gross_yield(funding_rate: Decimal, interval_hours: int) -> Decimal:
    if interval_hours <= 0:
        return Decimal("0")
    events_per_year = HOURS_PER_YEAR / Decimal(interval_hours)
    return funding_rate * events_per_year


def compute_total_cost(
    notional: Decimal,
    fee_buffer_bps: Decimal,
    slippage_buffer_bps: Decimal,
) -> Decimal:
    total_bps = fee_buffer_bps + slippage_buffer_bps
    return quantize_money(notional * total_bps / BPS_DENOMINATOR)


def compute_break_even_funding_rate(
    total_cost: Decimal,
    notional: Decimal,
    expected_funding_events: int,
) -> Decimal:
    if notional <= 0 or expected_funding_events <= 0:
        return Decimal("999")
    return total_cost / (notional * Decimal(expected_funding_events))


def compute_annualised_net_yield(
    gross_per_event: Decimal,
    total_cost: Decimal,
    notional: Decimal,
    expected_funding_events: int,
    interval_hours: int,
) -> Decimal:
    if notional <= 0 or expected_funding_events <= 0 or interval_hours <= 0:
        return Decimal("0")

    total_expected_gross = gross_per_event * Decimal(expected_funding_events)
    total_expected_net = total_expected_gross - total_cost

    holding_hours = Decimal(expected_funding_events * interval_hours)
    if holding_hours <= 0:
        return Decimal("0")

    holding_return = total_expected_net / notional
    return holding_return * (HOURS_PER_YEAR / holding_hours)
