from decimal import Decimal

from core.pnl import normalise_fx_pair, unrealised_pnl_account_currency


def test_normalise_fx_pair_accepts_common_aliases() -> None:
    assert normalise_fx_pair("USDJPY") == ("USD", "JPY")
    assert normalise_fx_pair("USD/JPY") == ("USD", "JPY")
    assert normalise_fx_pair("USDJPY=X") == ("USD", "JPY")


def test_unrealised_pnl_keeps_quote_usd_fx_in_usd() -> None:
    pnl = unrealised_pnl_account_currency(
        symbol="GBPUSD",
        asset_class="forex",
        quantity=Decimal("186286"),
        avg_entry_price=Decimal("1.32152793"),
        current_price=Decimal("1.32279"),
    )
    assert pnl.quantize(Decimal("0.01")) == Decimal("235.11")


def test_unrealised_pnl_converts_usdjpy_quote_pnl_to_usd() -> None:
    pnl = unrealised_pnl_account_currency(
        symbol="USDJPY",
        asset_class="forex",
        quantity=Decimal("1519"),
        avg_entry_price=Decimal("161.49203045"),
        current_price=Decimal("163.9"),
    )
    assert pnl.quantize(Decimal("0.01")) == Decimal("22.32")


def test_unrealised_pnl_leaves_equity_formula_unchanged() -> None:
    pnl = unrealised_pnl_account_currency(
        symbol="AAPL",
        asset_class="equity",
        quantity=Decimal("10"),
        avg_entry_price=Decimal("100"),
        current_price=Decimal("105"),
    )
    assert pnl == Decimal("50")
