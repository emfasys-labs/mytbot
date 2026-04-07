from __future__ import annotations


_TRIGGER_ALIASES: dict[str, str] = {
    "DXY": "usd_dxy",
    "USD_DXY": "usd_dxy",
    "VIX": "vix",
    "BTC": "btc_price",
    "BTC_PRICE": "btc_price",
    "OIL": "crude_oil_price",
    "CRUDE_OIL": "crude_oil_price",
    "CRUDE_OIL_PRICE": "crude_oil_price",
    "US10Y": "us_10yr_yield",
    "US_10YR_YIELD": "us_10yr_yield",
}


def canonical_symbol(symbol: str) -> str:
    s = (symbol or "").strip().upper().replace("-", "_").replace(".", "_").replace("/", "_")
    return s


def canonical_trigger_symbol(symbol: str) -> str:
    s = canonical_symbol(symbol)
    return _TRIGGER_ALIASES.get(s, s.lower())
