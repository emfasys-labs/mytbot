"""
tests/test_router_alpaca_split.py
==================================

Locks in the routing-fairness changes:

1. Hunter-mode + demand>0.15 → Alpaca (threshold lowered from 0.35).
2. Default A/B slice: ~20% of equity orders go to Alpaca even outside the
   hunter+demand branch, so the online quality model accumulates Alpaca
   evidence instead of being permanently zero-prior.
3. The carve-outs only apply to equity / ETF and only when both IBKR and
   Alpaca are eligible+permitted. Crypto routing is unchanged.
"""

from __future__ import annotations

from execution.router import SmartOrderRouter


def _router_with(brokers: list[str]) -> SmartOrderRouter:
    r = SmartOrderRouter(brokers)
    return r


def test_alpaca_hunter_demand_threshold_lowered_to_015() -> None:
    r = _router_with(["ibkr", "alpaca"])
    md = {"profile_mode": "hunter", "demand_score": 0.16, "equity_ab_split": False}
    assert r.route("equity", "AAPL", md) == "alpaca"


def test_alpaca_hunter_demand_below_threshold_still_ibkr() -> None:
    r = _router_with(["ibkr", "alpaca"])
    md = {"profile_mode": "hunter", "demand_score": 0.10, "equity_ab_split": False}
    assert r.route("equity", "AAPL", md) == "ibkr"


def test_ab_split_disabled_routes_to_ibkr() -> None:
    r = _router_with(["ibkr", "alpaca"])
    md = {"profile_mode": "trader", "demand_score": 0.0, "equity_ab_split": False}
    # Same symbol every call → deterministic IBKR
    for _ in range(20):
        assert r.route("equity", "MSFT", md) == "ibkr"


def test_ab_split_with_p_zero_routes_to_ibkr() -> None:
    r = _router_with(["ibkr", "alpaca"])
    md = {"profile_mode": "trader", "equity_ab_probability": 0.0}
    for sym in ("AAPL", "MSFT", "GOOG", "AMZN", "NVDA"):
        assert r.route("equity", sym, md) == "ibkr"


def test_ab_split_with_p_one_routes_to_alpaca() -> None:
    r = _router_with(["ibkr", "alpaca"])
    md = {"profile_mode": "trader", "equity_ab_probability": 1.0}
    for sym in ("AAPL", "MSFT", "GOOG"):
        assert r.route("equity", sym, md) == "alpaca"


def test_ab_split_default_distributes_some_to_alpaca() -> None:
    """Default 20% A/B should send a meaningful slice to Alpaca."""
    r = _router_with(["ibkr", "alpaca"])
    md: dict = {}
    routes = [r.route("equity", f"SYM{i:03d}", md) for i in range(100)]
    alpaca_count = sum(1 for v in routes if v == "alpaca")
    # Tolerant bounds around 20% (deterministic per-symbol seed makes this
    # a property test, not a flaky statistical one).
    assert 5 < alpaca_count < 50, f"expected ~20 alpaca routes, got {alpaca_count}"


def test_ab_split_same_symbol_routes_consistently() -> None:
    """Same symbol within a session must always route to the same venue."""
    r = _router_with(["ibkr", "alpaca"])
    md: dict = {}
    first = r.route("equity", "AAPL", md)
    for _ in range(10):
        assert r.route("equity", "AAPL", md) == first


def test_crypto_routing_unchanged_by_equity_ab_split() -> None:
    r = _router_with(["ibkr", "alpaca", "kraken", "binance"])
    md = {"profile_mode": "trader"}
    # Spot USD pair should still go to Kraken (existing rule)
    assert r.route("crypto", "BTC-USD", md) == "kraken"


def test_forex_routing_unchanged_uses_ibkr() -> None:
    r = _router_with(["ibkr", "alpaca"])
    md = {"profile_mode": "trader"}
    # Forex shouldn't get the A/B treatment — only equity/etf does
    for _ in range(10):
        assert r.route("forex", "EURUSD", md) == "ibkr"
