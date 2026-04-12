from decimal import Decimal

from strategies.arbitrage.calculator import (
    compute_annualised_gross_yield,
    compute_annualised_net_yield,
    compute_basis_bps,
    compute_break_even_funding_rate,
    compute_gross_funding_per_event,
    compute_total_cost,
)
from strategies.arbitrage.spread_calculator import compute_net_spread, compute_spread_bps


def test_compute_basis_bps():
    b = compute_basis_bps(Decimal("50100"), Decimal("50000"))
    assert b == Decimal("20")


def test_compute_gross_funding_per_event():
    g = compute_gross_funding_per_event(Decimal("10000"), Decimal("0.0003"))
    assert g == Decimal("3")


def test_compute_annualised_gross_yield():
    y = compute_annualised_gross_yield(Decimal("0.0003"), 8)
    assert y == Decimal("0.0003") * (Decimal("8760") / Decimal("8"))


def test_compute_total_cost():
    c = compute_total_cost(Decimal("10000"), Decimal("12"), Decimal("8"))
    assert c == Decimal("20")


def test_compute_break_even_funding_rate():
    total = Decimal("20")
    be = compute_break_even_funding_rate(total, Decimal("10000"), 10)
    assert be == Decimal("0.0002")


def test_compute_annualised_net_yield():
    gross = Decimal("15")
    cost = Decimal("10")
    net_annual = compute_annualised_net_yield(gross, cost, Decimal("10000"), 3, 8)
    assert net_annual > Decimal("0")


def test_spread_bps_and_net():
    bid = Decimal("50100")
    ask = Decimal("50000")
    assert compute_spread_bps(bid, ask) == Decimal("20")
    net = compute_net_spread(Decimal("10000"), bid, ask, Decimal("5"), Decimal("5"))
    assert net > 0
