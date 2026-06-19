from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

from run_m3 import _apply_signal_to_portfolio_state


def test_post_fill_snapshot_uses_ledger_remaining_quantity_for_partial_close():
    state = {
        "positions": {
            "BCH-USD": {
                "symbol": "BCH-USD",
                "broker": "bybit",
                "quantity": Decimal("164.37860267"),
                "avg_entry_price": Decimal("344.29793769"),
                "current_price": Decimal("344.29793769"),
                "asset_class": "crypto",
            }
        },
        "trades_today": 0,
    }
    signal = SimpleNamespace(
        symbol="BCH-USD",
        broker="bybit",
        side="sell",
        suggested_quantity=Decimal("124.78247192"),
        suggested_price=Decimal("236.30000305"),
        asset_class="crypto",
        metadata={},
    )
    result = SimpleNamespace(
        avg_fill_price=Decimal("236.30000305"),
        ledger_position_qty_after=Decimal("39.59613075"),
        ledger_avg_cost_basis=Decimal("344.29793769"),
    )

    _apply_signal_to_portfolio_state(state, signal, result)

    pos = state["positions"]["BCH-USD"]
    assert pos["quantity"] == Decimal("39.59613075")
    assert pos["avg_entry_price"] == Decimal("344.29793769")
    assert pos["current_price"] == Decimal("236.30000305")
    assert state["current_gross_exposure"] == Decimal("9356.5658169931987875")


def test_post_fill_snapshot_tombstones_ledger_flat_position():
    state = {
        "positions": {
            "bybit:BCH-USD": {
                "symbol": "BCH-USD",
                "broker": "bybit",
                "quantity": Decimal("-124.78247192"),
                "avg_entry_price": Decimal("236.30000305"),
                "current_price": Decimal("224"),
                "asset_class": "crypto",
            }
        },
        "trades_today": 0,
    }
    signal = SimpleNamespace(
        symbol="BCH-USD",
        broker="bybit",
        side="buy",
        suggested_quantity=Decimal("124.78247192"),
        suggested_price=Decimal("224"),
        asset_class="crypto",
        metadata={},
    )
    result = SimpleNamespace(
        avg_fill_price=Decimal("224"),
        ledger_position_qty_after=Decimal("0"),
        ledger_avg_cost_basis=Decimal("0"),
    )

    _apply_signal_to_portfolio_state(state, signal, result)

    assert state["positions"] == {}
    assert state["current_gross_exposure"] == Decimal("0")
    assert state["_closed_position_tombstones"][0]["symbol"] == "BCH-USD"


def test_post_fill_snapshot_uses_actual_execution_broker_after_reroute():
    state = {"positions": {}, "trades_today": 0}
    signal = SimpleNamespace(
        symbol="ATM-USD",
        broker="binance",
        side="buy",
        suggested_quantity=Decimal("30102.34798314"),
        suggested_price=Decimal("1.661"),
        asset_class="crypto",
        metadata={"broker": "binance", "crypto_venue_rerouted_from": ["binance"]},
    )
    result = SimpleNamespace(
        avg_fill_price=Decimal("1.661"),
        ledger_position_qty_after=Decimal("30102.34798314"),
        ledger_avg_cost_basis=Decimal("1.661"),
        execution_broker="bybit",
    )

    _apply_signal_to_portfolio_state(state, signal, result)

    pos = state["positions"]["ATM-USD"]
    assert pos["broker"] == "bybit"
    assert pos["quantity"] == Decimal("30102.34798314")
    assert pos["instrument_metadata"]["crypto_venue_rerouted_from"] == ["binance"]
