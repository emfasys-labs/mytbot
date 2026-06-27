from core.instrument_semantics import (
    canonical_economic_symbol,
    is_cash_equivalent_pair,
)


def test_cash_equivalent_pairs_are_not_directional_alpha() -> None:
    assert is_cash_equivalent_pair("USDT-USD")
    assert is_cash_equivalent_pair("USDC/USD")
    assert is_cash_equivalent_pair("RLUSD-USD")
    assert is_cash_equivalent_pair("USAT-USD")
    assert not is_cash_equivalent_pair("BTC-USD")


def test_wrapped_assets_share_underlying_exposure_symbol() -> None:
    assert canonical_economic_symbol("WBTC-USD") == "BTC-USD"
    assert canonical_economic_symbol("WBETH-USD") == "ETH-USD"
    assert canonical_economic_symbol("WETHUSDT") == "ETHUSDT"
    assert canonical_economic_symbol("LINK-USD") == "LINK-USD"
