"""Multi-asset-class strategy routing.

Covers the helpers in ``system/trading_loop/helpers.py`` that classify a
ticker as equity / crypto / forex / future, the per-broker symbol translation
that strips yfinance suffixes (``=X``, ``=F``) before orders reach the broker,
and the new ``Strategy.supports_asset_class`` declarative gate.
"""

from __future__ import annotations

import pytest

from strategies.mean_reversion import MeanReversionStrategy
from strategies.momentum import MomentumBreakoutStrategy
from system.trading_loop.helpers import (
    asset_class_for_symbol,
    broker_symbol_for,
    is_crypto_symbol,
    is_forex_symbol,
    is_futures_symbol,
)


@pytest.mark.parametrize(
    "symbol,expected",
    [
        ("SPY", "equity"),
        ("AAPL", "equity"),
        ("BTC-USD", "crypto"),
        ("ETH-USD", "crypto"),
        ("SOL-USD", "crypto"),
        ("EURUSD=X", "forex"),
        ("GBPUSD=X", "forex"),
        ("USDJPY=X", "forex"),
        ("ES=F", "future"),
        ("CL=F", "future"),
        ("GC=F", "future"),
    ],
)
def test_asset_class_for_symbol(symbol, expected):
    assert asset_class_for_symbol(symbol) == expected


def test_is_forex_symbol_strict():
    assert is_forex_symbol("EURUSD=X") is True
    assert is_forex_symbol("GBPUSD=X") is True
    # Too long / non-alpha → not forex
    assert is_forex_symbol("AAPL=X") is False          # 4-letter base
    assert is_forex_symbol("SPY") is False
    assert is_forex_symbol("BTC-USD") is False


def test_is_futures_symbol_strict():
    assert is_futures_symbol("ES=F") is True
    assert is_futures_symbol("NQ=F") is True
    assert is_futures_symbol("CL=F") is True
    assert is_futures_symbol("SPY") is False
    assert is_futures_symbol("EURUSD=X") is False


def test_is_crypto_symbol_still_works():
    assert is_crypto_symbol("BTC-USD") is True
    assert is_crypto_symbol("ETH-USDT") is True
    assert is_crypto_symbol("SPY") is False
    assert is_crypto_symbol("EURUSD=X") is False


def test_broker_symbol_strips_forex_suffix():
    assert broker_symbol_for("EURUSD=X", "ibkr") == "EURUSD"
    assert broker_symbol_for("GBPUSD=X", "ibkr") == "GBPUSD"


def test_broker_symbol_strips_futures_suffix():
    assert broker_symbol_for("ES=F", "ibkr") == "ES"


def test_broker_symbol_passthrough_for_equity_and_crypto():
    assert broker_symbol_for("SPY", "ibkr") == "SPY"
    assert broker_symbol_for("BTC-USD", "binance") == "BTC-USD"


def test_broker_symbol_alpaca_crypto_uses_slash():
    """Alpaca rejects BTC-USD with 'asset not found' — it expects BTC/USD."""
    assert broker_symbol_for("BTC-USD", "alpaca") == "BTC/USD"
    assert broker_symbol_for("ETH-USD", "alpaca") == "ETH/USD"
    assert broker_symbol_for("SOL-USD", "alpaca") == "SOL/USD"
    assert broker_symbol_for("XRP-USDT", "alpaca") == "XRP/USDT"
    assert broker_symbol_for("DOGE-USDC", "alpaca") == "DOGE/USDC"


def test_broker_symbol_alpaca_leaves_equity_with_hyphen_alone():
    """Alpaca tickers like BRK-B must keep their hyphen — those are equities,
    not crypto pairs. The translation only applies to -USD/-USDT/-USDC.
    """
    assert broker_symbol_for("BRK-B", "alpaca") == "BRK-B"
    assert broker_symbol_for("BF-B", "alpaca") == "BF-B"


def test_broker_symbol_alpaca_strips_forex_suffix_and_leaves_forex():
    """Forex stays a 6-char pair for any broker (including Alpaca, though
    Alpaca doesn't trade forex today — we still want a deterministic
    translation)."""
    assert broker_symbol_for("EURUSD=X", "alpaca") == "EURUSD"


def test_momentum_strategy_declares_multi_class():
    s = MomentumBreakoutStrategy({
        "asset_classes": ["equity", "crypto", "forex"],
    })
    assert s.supports_asset_class("equity") is True
    assert s.supports_asset_class("crypto") is True
    assert s.supports_asset_class("forex") is True
    assert s.supports_asset_class("future") is False
    # Primary scalar reflects first entry
    assert s.asset_class == "equity"


def test_mean_reversion_legacy_single_class_still_works():
    # Legacy config shape: single ``asset_class`` string with no list.
    s = MeanReversionStrategy({"asset_class": "equity"})
    assert s.supports_asset_class("equity") is True
    assert s.supports_asset_class("crypto") is False


def test_strategy_defaults_when_nothing_set():
    s = MeanReversionStrategy({})
    # Base default is equity-only; a bare config must not silently enable crypto.
    assert s.supports_asset_class("equity") is True
    assert s.supports_asset_class("crypto") is False
