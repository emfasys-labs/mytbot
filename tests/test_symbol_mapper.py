from data.symbol_mapper import canonical_symbol, kraken_pair_altname, to_venue_symbol


def test_canonical_symbol():
    assert canonical_symbol("btc/usdt") == "BTCUSDT"
    assert canonical_symbol("ETH-USDT") == "ETHUSDT"


def test_kraken_xbt():
    assert kraken_pair_altname("BTCUSDT") == "XBTUSDT"
    assert kraken_pair_altname("BTC/USD") == "XBTUSD"


def test_to_venue_symbol():
    assert to_venue_symbol("kraken", "BTCUSDT") == "XBTUSDT"
    assert to_venue_symbol("binance", "BTCUSDT") == "BTCUSDT"
    assert to_venue_symbol("bybit", "BTC/USDT") == "BTCUSDT"
