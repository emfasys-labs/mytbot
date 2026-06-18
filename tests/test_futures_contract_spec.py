"""D165 — futures contract spec table + helpers.

These are exchange-defined instrument facts (multiplier, exchange). The key
safety property is that bare roots that collide with equity tickers
(``CL`` = Colgate, ``ES`` = Eversource, ...) are NOT treated as futures — only
the yfinance continuous form ``ROOT=F`` is.
"""

from __future__ import annotations

from decimal import Decimal

from core.instruments import (
    FUTURES_CONTRACT_SPECS,
    futures_multiplier,
    futures_root,
    futures_spec_for,
)
from instruments.sources.static_futures import FUTURES_ROOTS


def test_futures_root_requires_continuous_suffix() -> None:
    assert futures_root("CL=F") == "CL"
    assert futures_root("es=f") == "ES"  # case-insensitive
    assert futures_root("ZN=F") == "ZN"


def test_bare_roots_are_not_futures_equity_collision_guard() -> None:
    # CL = Colgate-Palmolive, ES = Eversource, GC/SI/PA/PL/HG/CC/CT = real
    # equity tickers. Sizing must NOT treat these as futures.
    for bare in ("CL", "ES", "GC", "SI", "PA", "PL", "HG", "CC", "CT", "NG"):
        assert futures_root(bare) is None
        assert futures_spec_for(bare) is None
        assert futures_multiplier(bare) is None


def test_unknown_and_empty_symbols() -> None:
    assert futures_root("") is None
    assert futures_root("SPY") is None
    assert futures_root("AAPL=F") is None  # =F but not a known root
    assert futures_multiplier("AAPL") is None
    assert futures_multiplier("BTC-USD") is None


def test_known_specs_have_correct_facts() -> None:
    cl = futures_spec_for("CL=F")
    assert cl is not None
    assert cl.root == "CL"
    assert cl.exchange == "NYMEX"
    assert cl.multiplier == Decimal("1000")

    es = futures_spec_for("ES=F")
    assert es is not None and es.exchange == "CME" and es.multiplier == Decimal("50")

    gc = futures_spec_for("GC=F")
    assert gc is not None and gc.exchange == "COMEX" and gc.multiplier == Decimal("100")

    zn = futures_spec_for("ZN=F")
    assert zn is not None and zn.exchange == "CBOT" and zn.multiplier == Decimal("1000")


def test_futures_multiplier_values() -> None:
    assert futures_multiplier("ES=F") == Decimal("50")
    assert futures_multiplier("NQ=F") == Decimal("20")
    assert futures_multiplier("CL=F") == Decimal("1000")
    assert futures_multiplier("SI=F") == Decimal("5000")


def test_every_pipeline_root_has_a_spec() -> None:
    # Consistency: every root advertised by the registry source has a tradeable
    # spec, so no continuous future can slip through unsized.
    for root, _name, _group in FUTURES_ROOTS:
        assert root in FUTURES_CONTRACT_SPECS, f"missing spec for {root}"
        spec = FUTURES_CONTRACT_SPECS[root]
        assert spec.multiplier > 0
        assert spec.exchange
