from dataclasses import dataclass
from decimal import Decimal

from portfolio.broker_budget import (
    BrokerBudget,
    cap_orders_to_broker_budgets,
    compute_broker_budgets,
    existing_notional_by_broker,
)


@dataclass
class _Order:
    symbol: str
    broker: str
    delta_notional: Decimal
    net_conviction: Decimal
    reduce_only: bool = False
    close_only: bool = False


def test_capital_pct_applies_per_broker():
    budgets = compute_broker_budgets(
        {"binance": "1000", "ibkr": "10000"},
        Decimal("0.5"),
    )
    assert budgets["binance"].cap == Decimal("500")
    assert budgets["ibkr"].cap == Decimal("5000")
    assert budgets["binance"].room == Decimal("500")


def test_existing_notional_reduces_room():
    state = {
        "positions": {
            "BTC-USD": {"broker": "binance", "quantity": "0.1", "current_price": "2000"},
            "AAPL": {"broker": "ibkr", "quantity": "10", "current_price": "100"},
        }
    }
    existing = existing_notional_by_broker(state)
    assert existing["binance"] == Decimal("200")
    assert existing["ibkr"] == Decimal("1000")
    budgets = compute_broker_budgets({"binance": "1000"}, Decimal("1.0"), existing)
    assert budgets["binance"].room == Decimal("800")


def test_cap_funds_strongest_conviction_first_and_shrinks():
    budgets = {"binance": BrokerBudget("binance", Decimal("1000"), Decimal("1.0"))}
    orders = [
        _Order("WEAK", "binance", Decimal("700"), Decimal("0.2")),
        _Order("STRONG", "binance", Decimal("700"), Decimal("0.9")),
    ]
    kept, diag = cap_orders_to_broker_budgets(orders, budgets)
    by_sym = {o.symbol: o for o in kept}
    # Strong funded fully (700), weak shrunk to remaining 300.
    assert by_sym["STRONG"].delta_notional == Decimal("700")
    assert by_sym["WEAK"].delta_notional == Decimal("300")
    assert diag["shrunk"] == 1


def test_reduce_orders_always_pass():
    budgets = {"binance": BrokerBudget("binance", Decimal("0"), Decimal("1.0"))}
    orders = [_Order("BTC-USD", "binance", Decimal("500"), Decimal("0.5"), reduce_only=True)]
    kept, diag = cap_orders_to_broker_budgets(orders, budgets)
    assert len(kept) == 1
    assert diag["dropped"] == 0


def test_unknown_broker_not_blocked():
    budgets = {"binance": BrokerBudget("binance", Decimal("1000"), Decimal("1.0"))}
    orders = [_Order("XYZ", "kraken", Decimal("500"), Decimal("0.5"))]
    kept, diag = cap_orders_to_broker_budgets(orders, budgets)
    assert len(kept) == 1
    assert diag["uncapped_no_budget"] == 1
