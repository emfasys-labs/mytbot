from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

from ai.regime import filter_by_allowed_strategies
from run_m5 import (
    _apply_filled_result_to_portfolio_state,
    _build_broker_configs,
    _build_parser,
    _estimate_realized_pnl_from_fill,
)


def test_build_broker_configs_has_expected_brokers():
    cfg = _build_broker_configs()
    assert "ibkr" in cfg
    assert "kraken" in cfg
    assert "binance" in cfg
    assert "bybit" in cfg
    assert "alpaca" in cfg


def test_parser_supports_reconcile_only_flag():
    p = _build_parser()
    args = p.parse_args(["--reconcile-only"])
    assert args.reconcile_only is True


def test_parser_supports_ai_config_flag():
    p = _build_parser()
    args = p.parse_args(["--ai-config", "config/ai.yaml"])
    assert args.ai_config == "config/ai.yaml"


def test_filter_by_regime():
    a = SimpleNamespace(strategy="momentum_breakout")
    b = SimpleNamespace(strategy="mean_reversion")
    out = filter_by_allowed_strategies([a, b], {"mean_reversion"})
    assert len(out) == 1
    assert out[0].strategy == "mean_reversion"


def test_apply_filled_result_uses_actual_fill_quantity_and_price():
    state = {
        "positions": {},
        "symbol_exposure": {},
        "asset_class_exposure": {},
        "current_gross_exposure": Decimal("0"),
        "trades_today": 0,
    }
    signal = SimpleNamespace(symbol="SPY", side="buy", asset_class="equity", broker="ibkr", suggested_price=Decimal("100"))
    result = SimpleNamespace(filled_quantity=Decimal("8"), avg_fill_price=Decimal("102"))
    _apply_filled_result_to_portfolio_state(state, signal, result)
    pos = state["positions"]["SPY"]
    assert Decimal(str(pos["quantity"])) == Decimal("8")
    assert Decimal(str(pos["avg_entry_price"])) == Decimal("102")
    assert state["trades_today"] == 1


def test_estimate_realized_pnl_from_fill_for_partial_close():
    state = {
        "positions": {
            "SPY": {
                "quantity": Decimal("10"),
                "avg_entry_price": Decimal("100"),
                "current_price": Decimal("101"),
            }
        }
    }
    signal = SimpleNamespace(symbol="SPY", side="sell", suggested_price=Decimal("102"))
    result = SimpleNamespace(filled_quantity=Decimal("4"), avg_fill_price=Decimal("103"))
    pnl = _estimate_realized_pnl_from_fill(state, signal, result)
    assert pnl == Decimal("12")


def test_estimate_realized_pnl_uses_broker_symbol_position_key_and_case():
    state = {
        "positions": {
            "ibkr:SPY": {
                "symbol": "SPY",
                "broker": "ibkr",
                "quantity": Decimal("10"),
                "avg_entry_price": Decimal("100"),
                "current_price": Decimal("101"),
            }
        }
    }
    signal = SimpleNamespace(symbol="SPY", broker="ibkr", side="SELL", suggested_price=Decimal("102"))
    result = SimpleNamespace(filled_quantity=Decimal("4"), avg_fill_price=Decimal("103"))
    pnl = _estimate_realized_pnl_from_fill(state, signal, result)
    assert pnl == Decimal("12")
