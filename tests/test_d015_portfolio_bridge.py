from datetime import datetime, timezone
from decimal import Decimal

from system.d015_portfolio_bridge import portfolio_dict_to_runtime_state


def test_portfolio_dict_to_runtime_state_maps_positions() -> None:
    d = {
        "portfolio_value": Decimal("100000"),
        "tradable_capital": Decimal("80000"),
        "current_gross_exposure": Decimal("20000"),
        "positions": {
            "SPY": {
                "quantity": Decimal("10"),
                "avg_entry_price": Decimal("400"),
                "current_price": Decimal("410"),
                "asset_class": "equity",
                "broker": "ibkr",
            }
        },
    }
    ps = portfolio_dict_to_runtime_state(d, mode="trader", capital_pct=0.8, now=datetime.now(timezone.utc))
    assert ps.nav == Decimal("100000")
    assert len(ps.positions) == 1
    assert ps.positions[0].symbol == "SPY"
    assert ps.positions[0].market_value == Decimal("4100")
