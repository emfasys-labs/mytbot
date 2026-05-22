"""Tests for ``instruments.canonical`` symbol normalisation (D116)."""

from __future__ import annotations

import pytest

from instruments.canonical import (
    canonical_to_broker,
    detect_asset_class,
    from_broker_symbol,
    to_canonical,
)


def test_to_canonical_us_equity_round_trip() -> None:
    parsed = to_canonical("AAPL", broker="alpaca")
    assert parsed is not None
    assert parsed.symbol == "AAPL"
    assert parsed.asset_class == "equity"
    assert parsed.region == "US"
    assert canonical_to_broker(parsed, "alpaca") == "AAPL"


def test_to_canonical_lse_equity_keeps_suffix() -> None:
    parsed = to_canonical("HSBA.L", broker="ibkr")
    assert parsed is not None
    assert parsed.symbol == "HSBA.L"
    assert parsed.region == "UK"
    assert parsed.exchange == "LSE"
    assert parsed.suffix == ".L"
    # Alpaca does not support LSE → translation refuses rather than guessing
    assert canonical_to_broker(parsed, "alpaca") is None


def test_to_canonical_xetra_japan_hk() -> None:
    assert to_canonical("SAP.DE").symbol == "SAP.DE"
    assert to_canonical("7203.T").symbol == "7203.T"
    assert to_canonical("0700.HK").symbol == "0700.HK"


def test_kraken_xbt_normalises_to_btc_usd() -> None:
    parsed = to_canonical("XBT/USD", broker="kraken")
    assert parsed is not None
    assert parsed.symbol == "BTC-USD"
    assert parsed.asset_class == "crypto"


def test_binance_usdt_normalises_to_canonical_usd() -> None:
    parsed = to_canonical("BTCUSDT", broker="binance")
    assert parsed is not None
    assert parsed.symbol == "BTC-USD"


def test_canonical_to_broker_crypto_routes() -> None:
    assert canonical_to_broker("BTC-USD", "kraken") == "XBT/USD"
    assert canonical_to_broker("ETH-USD", "kraken") == "ETH/USD"
    assert canonical_to_broker("ETH-USD", "binance") == "ETHUSDT"
    assert canonical_to_broker("SOL-USD", "bybit") == "SOLUSDT"
    assert canonical_to_broker("BTC-USD", "ibkr") == "BTC"
    assert canonical_to_broker("SOL-USD", "ibkr") == "SOL"
    assert canonical_to_broker("AAVE-USD", "ibkr") is None


def test_ibkr_dot_forex_round_trips() -> None:
    parsed = to_canonical("EUR.USD", broker="ibkr")
    assert parsed is not None
    assert parsed.symbol == "EURUSD=X"
    assert parsed.asset_class == "fx"
    assert canonical_to_broker(parsed, "ibkr") == "EUR.USD"


def test_yfinance_fx_pair_round_trips() -> None:
    parsed = to_canonical("EURUSD=X")
    assert parsed is not None
    assert parsed.symbol == "EURUSD=X"
    assert parsed.asset_class == "fx"
    assert canonical_to_broker(parsed, "ibkr") == "EUR.USD"


def test_unsupported_input_returns_none() -> None:
    assert to_canonical("") is None
    assert to_canonical("   ") is None
    assert to_canonical("123$$$") is None


def test_detect_asset_class_handles_known_forms() -> None:
    assert detect_asset_class("AAPL") == "equity"
    assert detect_asset_class("ES=F") == "future"
    assert detect_asset_class("EURUSD=X") == "fx"
    assert detect_asset_class("BTC-USD") == "crypto"


def test_from_broker_symbol_wrapper() -> None:
    assert from_broker_symbol("XBT/USD", "kraken") == "BTC-USD"
    assert from_broker_symbol("EUR.USD", "ibkr") == "EURUSD=X"
    assert from_broker_symbol("AAPL", "alpaca") == "AAPL"


def test_universe_builder_to_yf_symbol_uses_canonical() -> None:
    from data.universe_builder import _to_yf_symbol

    assert _to_yf_symbol("XBT/USD", "kraken") == "BTC-USD"
    assert _to_yf_symbol("EUR.USD", "ibkr") == "EURUSD=X"
    assert _to_yf_symbol("AAPL", "alpaca") == "AAPL"
    assert _to_yf_symbol("HSBA.L", "ibkr") == "HSBA.L"
