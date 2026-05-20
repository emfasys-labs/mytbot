"""
tests/test_equity_index_cluster.py
===================================
D115 — Broad-market equity index cluster cap.

Symmetric to the FX cluster test: SPY long + QQQ short + IWM short on
2026-05-19 were three correlated bets on the same systematic US equity
factor, and the system sized each independently. This cap bounds the
aggregate signed cluster notional.
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
        "equity_index_cluster": {
            "enabled": True,
            "max_net_exposure_pct": "0.20",
            "symbols": ["SPY", "QQQ", "IWM", "DIA", "VTI"],
        },
    }
    cfg.update(overrides)
    return RiskEngine(cfg)


def _signal(*, symbol, side, qty, price, asset_class="equity") -> Signal:
    return Signal(
        signal_id=f"sig_{symbol}_{side}",
        symbol=symbol,
        side=side,
        strategy="volatility_regime",
        confidence=0.8,
        suggested_quantity=Decimal(qty),
        suggested_price=Decimal(price),
        broker="alpaca",
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


def test_non_cluster_symbol_skipped():
    eng = _engine()
    sig = _signal(symbol="AAPL", side="buy", qty="100", price="300.0")
    decision = eng.evaluate(sig, _portfolio(nav="1000000"))
    assert decision.verdict.value == "approved"


def test_first_cluster_position_within_cap_allowed():
    eng = _engine()
    sig = _signal(symbol="SPY", side="buy", qty="200", price="730.0")  # $146k
    decision = eng.evaluate(sig, _portfolio(nav="1000000"))
    assert decision.verdict.value == "approved"


def test_adding_to_cluster_above_cap_rejected():
    eng = _engine()
    positions = {
        "SPY": {"quantity": "300", "current_price": "730.0"},  # +219,000
    }
    # IWM short adds magnitude with opposite sign — but |projected| might
    # still exceed cap. Use a clear additive case: another long like QQQ.
    sig = _signal(symbol="QQQ", side="buy", qty="100", price="700.0")  # +70,000
    decision = eng.evaluate(sig, _portfolio(nav="1000000", positions=positions))
    # 219 + 70 = 289 > 20% of 1M = 200. Reject.
    assert decision.verdict.value == "rejected"
    assert "equity_index_cluster" in (decision.reason or "")


def test_opposite_direction_cluster_leg_neutralises():
    eng = _engine()
    positions = {
        "SPY": {"quantity": "300", "current_price": "730.0"},   # +219,000
    }
    # QQQ short reduces |signed cluster| toward 0.
    sig = _signal(symbol="QQQ", side="sell", qty="100", price="700.0")  # -70,000
    decision = eng.evaluate(sig, _portfolio(nav="1000000", positions=positions))
    assert decision.verdict.value == "approved"


def test_reduce_only_never_blocked():
    eng = _engine()
    positions = {
        "SPY": {"quantity": "500", "current_price": "730.0"},   # +365,000 already past cap
    }
    sig = _signal(symbol="SPY", side="sell", qty="500", price="730.0")
    sig.metadata = {"reduce_only": True}
    decision = eng.evaluate(sig, _portfolio(nav="1000000", positions=positions))
    assert decision.verdict.value == "approved"


def test_disabled_cluster_check_allows_anything():
    eng = _engine(equity_index_cluster={"enabled": False, "symbols": ["SPY", "QQQ"]})
    positions = {
        "SPY": {"quantity": "1000", "current_price": "730.0"},  # +730,000 (way over)
    }
    sig = _signal(symbol="QQQ", side="buy", qty="500", price="700.0")
    decision = eng.evaluate(sig, _portfolio(nav="1000000", positions=positions))
    assert decision.verdict.value == "approved"


def test_dynamic_cap_shrinks_to_zero_on_terrible_market_state():
    eng = _engine()
    positions = {
        "SPY": {"quantity": "1", "current_price": "100.0"},  # tiny exposure
    }
    sig = _signal(symbol="QQQ", side="buy", qty="100", price="700.0")
    
    # Portfolio metadata explicitly reports a terrible market state (0.01)
    port = _portfolio(nav="1000000", positions=positions)
    port["metadata"] = {"market_state_score": 0.01}
    
    decision = eng.evaluate(sig, port)
    # Cap is 1% of 1M = 10k. 70k is well above cap.
    assert decision.verdict.value == "rejected"
    assert "equity_index_cluster" in (decision.reason or "")


def test_dynamic_cap_expands_on_excellent_market_state():
    eng = _engine()
    positions = {
        "SPY": {"quantity": "1000", "current_price": "500.0"},  # +500,000 (50% of NAV)
    }
    sig = _signal(symbol="QQQ", side="buy", qty="100", price="500.0")  # +50,000 -> 550,000 total
    
    # Portfolio metadata explicitly reports an excellent market state (0.80) -> 80% cap
    port = _portfolio(nav="1000000", positions=positions)
    port["metadata"] = {"market_state_score": 0.80}
    
    decision = eng.evaluate(sig, port)
    # Cap is 80% of 1M = 800k. 550k is well within cap.
    assert decision.verdict.value == "approved"

