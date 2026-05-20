"""
tests/test_fx_cluster_exposure.py
==================================
D115 — FX directional-cluster exposure cap.

On 2026-05-19 the system held six FX positions all betting the same way
(short USD): EURUSD long, GBPUSD long, AUDUSD long, USDCAD short,
USDCHF short, USDJPY short. The dollar rallied, every leg lost. The
risk engine had no cluster awareness — each leg was sized independently.

This test suite proves that:
    * The pair-orientation helper correctly classifies USDxxx vs xxxUSD.
    * Adding a 7th same-direction USD leg is rejected when the cap is set.
    * Neutralising legs (reduce-only or opposite-direction) are NEVER blocked.
    * Non-FX symbols are not affected.
    * Disabling the cluster cap restores legacy behaviour.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from risk.engine import RiskEngine
from signals.engine import Signal


def _engine(**overrides) -> RiskEngine:
    cfg = {
        "min_signal_confidence": 0.0,
        "min_trade_quality_score": 0.0,
        "theme_uniqueness_check": False,
        "require_catalyst": False,
        "enforce_static_exposure_caps": False,
        "enforce_static_order_caps": False,
        "max_consecutive_losses": 999,
        "max_daily_loss_pct": 1.0,
        "max_drawdown_pct": 1.0,
        "max_loss_per_trade_pct": 1.0,
        "fx_cluster": {"enabled": True, "max_usd_directional_exposure_pct": "0.15"},
    }
    cfg.update(overrides)
    return RiskEngine(cfg)


def _signal(
    *,
    symbol: str,
    side: str,
    qty: str,
    price: str,
    asset_class: str = "forex",
    confidence: float = 0.8,
) -> Signal:
    return Signal(
        signal_id=f"sig_{symbol}_{side}",
        symbol=symbol,
        side=side,
        strategy="event_driven_news",
        confidence=confidence,
        suggested_quantity=Decimal(qty),
        suggested_price=Decimal(price),
        broker="ibkr",
        asset_class=asset_class,
        timestamp="2026-05-19T13:00:00Z",
        metadata={},
    )


def _portfolio(nav: str, positions: dict | None = None) -> dict:
    return {
        "portfolio_value": Decimal(nav),
        "tradable_capital": Decimal(nav),
        "exposure_total": Decimal("0"),
        "asset_class_exposure": {},
        "positions": positions or {},
    }


# ---------------- orientation helper ----------------
def test_pair_orientation_classifies_usd_position():
    assert RiskEngine._fx_pair_orientation("USDJPY") == 1
    assert RiskEngine._fx_pair_orientation("USDCHF") == 1
    assert RiskEngine._fx_pair_orientation("EURUSD") == -1
    assert RiskEngine._fx_pair_orientation("AUDUSD") == -1
    assert RiskEngine._fx_pair_orientation("EURGBP") == 0
    # Yahoo-style suffixes / dashes
    assert RiskEngine._fx_pair_orientation("EURUSD=X") == -1
    assert RiskEngine._fx_pair_orientation("USDJPY=X") == 1
    assert RiskEngine._fx_pair_orientation("EUR/USD") == -1
    # No USD anywhere
    assert RiskEngine._fx_pair_orientation("BTC-USD") == -1  # crypto pair, but
    # we don't reach this helper for non-forex asset class anyway.


def test_position_usd_exposure_signs_match_intuition():
    # Magnitude uses mytbot's system-wide |qty * current_price| convention
    # (the same one as exposure/cash-deployed/held-edge calculations).
    # The cluster cap is calibrated against the same convention, so this
    # is internally consistent even when broker FX quote semantics differ.
    eurusd_long = {"quantity": "10000", "current_price": "1.16"}
    assert RiskEngine._fx_usd_exposure_from_position("EURUSD", eurusd_long) == Decimal("-11600.00")
    eurusd_short = {"quantity": "-10000", "current_price": "1.16"}
    assert RiskEngine._fx_usd_exposure_from_position("EURUSD", eurusd_short) == Decimal("11600.00")
    usdjpy_long = {"quantity": "1000", "current_price": "158.0"}
    assert RiskEngine._fx_usd_exposure_from_position("USDJPY", usdjpy_long) == Decimal("158000.0")
    usdjpy_short = {"quantity": "-1000", "current_price": "158.0"}
    assert RiskEngine._fx_usd_exposure_from_position("USDJPY", usdjpy_short) == Decimal("-158000.0")


# ---------------- end-to-end check ----------------
def test_fresh_fx_signal_within_cap_allowed():
    eng = _engine()
    sig = _signal(symbol="EURUSD", side="buy", qty="50000", price="1.16")
    decision = eng.evaluate(sig, _portfolio(nav="1000000"))
    assert decision.verdict.value == "approved"


def test_seventh_same_direction_leg_rejected_at_cap():
    eng = _engine()
    # Five existing short-USD positions ≈ -$120k. Cap at 15% of $1M = $150k.
    positions = {
        "EURUSD": {"quantity": "20000", "current_price": "1.16"},   # -23,200
        "GBPUSD": {"quantity": "15000", "current_price": "1.34"},   # -20,100
        "AUDUSD": {"quantity": "30000", "current_price": "0.71"},   # -21,300
        "USDCAD": {"quantity": "-20000", "current_price": "1.37"},  # -27,400
        "USDCHF": {"quantity": "-25000", "current_price": "0.79"},  # -19,750
    }
    # Sum ≈ -111,750. A new EURUSD long for $50k more would push to -$169k.
    sig = _signal(symbol="EURUSD", side="buy", qty="50000", price="1.16")
    decision = eng.evaluate(sig, _portfolio(nav="1000000", positions=positions))
    assert decision.verdict.value == "rejected"
    assert "fx_cluster" in (decision.reason or "")


def test_neutralising_leg_always_allowed():
    eng = _engine()
    # Already long USD heavily (-USDJPY would normally be SHORT USD,
    # so to get a LONG USD bias we use USDJPY long).
    positions = {
        "USDJPY": {"quantity": "500", "current_price": "158.0"},  # +79,000 (long USD)
    }
    # New EURUSD long (short USD) reduces |signed sum| from 79,000 toward 0.
    sig = _signal(symbol="EURUSD", side="buy", qty="50000", price="1.16")
    decision = eng.evaluate(sig, _portfolio(nav="1000000", positions=positions))
    assert decision.verdict.value == "approved"


def test_reduce_only_signal_skips_cluster_check():
    eng = _engine()
    positions = {
        "EURUSD": {"quantity": "200000", "current_price": "1.16"},
        "GBPUSD": {"quantity": "150000", "current_price": "1.34"},
    }
    sig = _signal(symbol="EURUSD", side="sell", qty="200000", price="1.16")
    sig.metadata = {"reduce_only": True}
    decision = eng.evaluate(sig, _portfolio(nav="1000000", positions=positions))
    assert decision.verdict.value == "approved"


def test_non_forex_symbol_skips_cluster_check():
    eng = _engine()
    positions = {
        "EURUSD": {"quantity": "500000", "current_price": "1.16"},  # heavily short USD
    }
    sig = _signal(symbol="AAPL", side="buy", qty="100", price="300.0", asset_class="equity")
    decision = eng.evaluate(sig, _portfolio(nav="1000000", positions=positions))
    assert decision.verdict.value == "approved"


def test_disabled_cluster_check_allows_anything():
    eng = _engine(fx_cluster={"enabled": False, "max_usd_directional_exposure_pct": "0.05"})
    positions = {
        "EURUSD": {"quantity": "500000", "current_price": "1.16"},  # -580,000
    }
    sig = _signal(symbol="GBPUSD", side="buy", qty="500000", price="1.34")
    decision = eng.evaluate(sig, _portfolio(nav="1000000", positions=positions))
    assert decision.verdict.value == "approved"


def test_eurgbp_does_not_count_against_usd_cluster():
    eng = _engine()
    positions = {
        "EURUSD": {"quantity": "50000", "current_price": "1.16"},
    }
    sig = _signal(symbol="EURGBP", side="buy", qty="100000", price="0.86")
    decision = eng.evaluate(sig, _portfolio(nav="1000000", positions=positions))
    # EURGBP has no USD leg so it cannot push the cluster.
    assert decision.verdict.value == "approved"

def test_dynamic_fx_cluster_cap_scales_with_market_state_score_high():
    eng = _engine()
    positions = {
        "EURUSD": {"quantity": "200000", "current_price": "1.16"},
    }
    sig = _signal(symbol="GBPUSD", side="buy", qty="50000", price="1.34")
    port = _portfolio(nav="1000000", positions=positions)
    port["metadata"] = {"market_state_score": 0.50}
    decision = eng.evaluate(sig, port)
    assert decision.verdict.value == "approved"

def test_dynamic_fx_cluster_cap_shrinks_with_market_state_score_low():
    eng = _engine()
    positions = {
        "EURUSD": {"quantity": "50000", "current_price": "1.16"},
    }
    sig = _signal(symbol="GBPUSD", side="buy", qty="50000", price="1.34")
    port = _portfolio(nav="1000000", positions=positions)
    port["metadata"] = {"market_state_score": 0.05}
    decision = eng.evaluate(sig, port)
    assert decision.verdict.value == "rejected"
