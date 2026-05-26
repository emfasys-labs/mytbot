"""
tests/test_stale_price_gate.py
===============================
D115 — Paper-mode stale-price gate.

Today's bleed pattern: signal generated at AAPL=$297.46 several minutes
ago, by the time the order reaches the paper-fill path AAPL is at
$298.21, but the simulated fill ignores the new market and locks in
$297.46. Five rounds of this and the basket has paid the round-trip
fee+slippage with no real edge captured.

The gate fetches the broker's current quote and refuses paper fills
that have drifted against the trade direction by more than the
configured threshold. Reduce-only / close intents are exempt.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock

import pytest

from brokers.base import (
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
)
from control.runtime import set_risk_engine
from execution.engine import ExecutionEngine
from risk.engine import RiskEngine
from signals.engine import Signal


def _engine() -> ExecutionEngine:
    risk_cfg = {
        "stale_price_gate": {"enabled": True, "max_adverse_drift_bps": 25},
        "paper_fee_bps": 0,
        "paper_slippage_bps": 0,
    }
    risk = RiskEngine(risk_cfg)
    set_risk_engine(risk)
    return ExecutionEngine(broker_configs={}, paper_mode=True)


def _signal(*, symbol="AAPL", side="buy", suggested_price="300.0", qty="100") -> Signal:
    return Signal(
        signal_id=f"sig_{symbol}_{side}",
        symbol=symbol,
        side=side,
        strategy="volatility_regime",
        confidence=0.8,
        suggested_quantity=Decimal(qty),
        suggested_price=Decimal(suggested_price),
        broker="ibkr",
        asset_class="equity",
        timestamp="2026-05-19T13:00:00Z",
        metadata={},
    )


def _order(*, symbol="AAPL", side=OrderSide.BUY, qty="100", limit=None) -> Order:
    return Order(
        symbol=symbol,
        side=side,
        order_type=OrderType.MARKET if limit is None else OrderType.LIMIT,
        quantity=Decimal(qty),
        client_order_id="cid-1",
        limit_price=Decimal(limit) if limit else None,
    )


class _FakeBroker:
    def __init__(self, last_price: str):
        self._last_price = Decimal(last_price)

    async def get_last_price(self, symbol):  # noqa: ARG002
        return self._last_price


# ---------------- gate behaviour ----------------
@pytest.mark.asyncio
async def test_stale_price_rejects_buy_when_market_up_more_than_threshold():
    eng = _engine()
    sig = _signal(symbol="AAPL", side="buy", suggested_price="300.00")
    order = _order(symbol="AAPL", side=OrderSide.BUY)
    # Suggested 300.00, market now 301.00 → +33bps adverse (BUY, market up).
    broker = _FakeBroker("301.00")
    result = await eng._simulate_fill(order, sig, broker=broker)
    assert result.status == OrderStatus.REJECTED
    assert result.filled_quantity == Decimal("0")
    assert "stale_signal_price" in (eng.last_skip_reason or "")


@pytest.mark.asyncio
async def test_stale_price_rejects_sell_when_market_down_more_than_threshold():
    eng = _engine()
    sig = _signal(symbol="AAPL", side="sell", suggested_price="300.00")
    order = _order(symbol="AAPL", side=OrderSide.SELL)
    broker = _FakeBroker("298.50")  # 50bps adverse for SELL (market down)
    result = await eng._simulate_fill(order, sig, broker=broker)
    assert result.status == OrderStatus.REJECTED


@pytest.mark.asyncio
async def test_favorable_drift_is_filled():
    eng = _engine()
    sig = _signal(symbol="AAPL", side="buy", suggested_price="300.00")
    order = _order(symbol="AAPL", side=OrderSide.BUY)
    # Market dropped: BUY filled below signal price = favorable.
    broker = _FakeBroker("299.00")
    result = await eng._simulate_fill(order, sig, broker=broker)
    assert result.status == OrderStatus.FILLED


@pytest.mark.asyncio
async def test_drift_within_threshold_is_filled():
    eng = _engine()
    sig = _signal(symbol="AAPL", side="buy", suggested_price="300.00")
    order = _order(symbol="AAPL", side=OrderSide.BUY)
    # +10 bps adverse — under the 25 bps threshold.
    broker = _FakeBroker("300.30")
    result = await eng._simulate_fill(order, sig, broker=broker)
    assert result.status == OrderStatus.FILLED


@pytest.mark.asyncio
async def test_reduce_only_signal_never_blocked_by_stale_gate():
    eng = _engine()
    sig = _signal(symbol="AAPL", side="sell", suggested_price="300.00")
    sig.metadata = {"reduce_only": True}
    order = _order(symbol="AAPL", side=OrderSide.SELL)
    # Wide adverse drift, but reduce-only is exempt.
    broker = _FakeBroker("295.00")
    result = await eng._simulate_fill(order, sig, broker=broker)
    assert result.status == OrderStatus.FILLED


@pytest.mark.asyncio
async def test_disabled_gate_allows_stale_fill():
    risk_cfg = {
        "stale_price_gate": {"enabled": False, "max_adverse_drift_bps": 25},
        "paper_fee_bps": 0,
        "paper_slippage_bps": 0,
    }
    set_risk_engine(RiskEngine(risk_cfg))
    eng = ExecutionEngine(broker_configs={}, paper_mode=True)
    sig = _signal(symbol="AAPL", side="buy", suggested_price="300.00")
    order = _order(symbol="AAPL", side=OrderSide.BUY)
    broker = _FakeBroker("305.00")  # 167 bps adverse but gate disabled
    result = await eng._simulate_fill(order, sig, broker=broker)
    assert result.status == OrderStatus.FILLED


@pytest.mark.asyncio
async def test_no_broker_quote_does_not_block():
    """When the broker has no quote, the gate cannot decide — fall through
    so existing paper-fill behaviour is preserved (no false rejects)."""
    eng = _engine()
    sig = _signal(symbol="AAPL", side="buy", suggested_price="300.00")
    order = _order(symbol="AAPL", side=OrderSide.BUY)
    # No broker passed → _broker_last_price returns None
    result = await eng._simulate_fill(order, sig, broker=None)
    assert result.status == OrderStatus.FILLED


@pytest.mark.asyncio
async def test_dynamic_volatility_expands_drift_threshold():
    eng = _engine()
    sig = _signal(symbol="TSLA", side="buy", suggested_price="300.00")
    
    # 33 bps drift. Normally 25 bps is the max -> rejected.
    broker = _FakeBroker("301.00")
    result1 = await eng._simulate_fill(_order(symbol="TSLA", side=OrderSide.BUY), sig, broker=broker)
    assert result1.status == OrderStatus.REJECTED
    
    # Now provide a volatility scalar of 2.0 (e.g. TSLA is twice as volatile)
    # The max threshold becomes 25 * 2.0 = 50 bps. 33 bps is now within limits.
    sig.metadata = {"symbol_volatility_scalar": 2.0}
    result2 = await eng._simulate_fill(_order(symbol="TSLA", side=OrderSide.BUY), sig, broker=broker)
    assert result2.status == OrderStatus.FILLED


# ---------------- per-asset-class override ----------------
def _engine_with_cfg(cfg: dict) -> ExecutionEngine:
    set_risk_engine(RiskEngine(cfg))
    return ExecutionEngine(broker_configs={}, paper_mode=True)


@pytest.mark.asyncio
async def test_crypto_uses_per_asset_class_override():
    """Crypto threshold (75 bps) must be applied to a crypto signal even when
    the equity-tuned global default (25 bps) would have rejected the fill."""
    cfg = {
        "stale_price_gate": {
            "enabled": True,
            "max_adverse_drift_bps": 25,
            "per_asset_class": {"crypto": 75},
        },
        "paper_fee_bps": 0,
        "paper_slippage_bps": 0,
    }
    eng = _engine_with_cfg(cfg)
    sig = _signal(symbol="AAVE-USD", side="sell", suggested_price="87.21")
    sig.asset_class = "crypto"
    order = _order(symbol="AAVE-USD", side=OrderSide.SELL)
    # 70 bps adverse drift — above global 25 bps but inside crypto 75 bps.
    broker = _FakeBroker("86.60")
    result = await eng._simulate_fill(order, sig, broker=broker)
    assert result.status == OrderStatus.FILLED


@pytest.mark.asyncio
async def test_equity_keeps_global_threshold_when_no_override():
    """A signal whose asset class has no per-class override falls through to
    the global ``max_adverse_drift_bps`` — never silently expanded."""
    cfg = {
        "stale_price_gate": {
            "enabled": True,
            "max_adverse_drift_bps": 25,
            "per_asset_class": {"crypto": 75},
        },
        "paper_fee_bps": 0,
        "paper_slippage_bps": 0,
    }
    eng = _engine_with_cfg(cfg)
    sig = _signal(symbol="AAPL", side="buy", suggested_price="300.00")
    sig.asset_class = "equity"
    order = _order(symbol="AAPL", side=OrderSide.BUY)
    broker = _FakeBroker("301.00")  # +33 bps adverse
    result = await eng._simulate_fill(order, sig, broker=broker)
    assert result.status == OrderStatus.REJECTED


@pytest.mark.asyncio
async def test_missing_config_keys_disable_gate_no_hardcoded_fallback():
    """If neither ``max_adverse_drift_bps`` nor any ``per_asset_class`` entry
    is supplied, the gate refuses to invent a constant and turns itself off
    (fail-open). This is the contract: zero hardcoded thresholds in code."""
    cfg = {
        "stale_price_gate": {"enabled": True},  # no thresholds at all
        "paper_fee_bps": 0,
        "paper_slippage_bps": 0,
    }
    eng = _engine_with_cfg(cfg)
    sig = _signal(symbol="AAPL", side="buy", suggested_price="300.00")
    sig.asset_class = "equity"
    order = _order(symbol="AAPL", side=OrderSide.BUY)
    broker = _FakeBroker("310.00")  # 333 bps adverse — would normally reject
    result = await eng._simulate_fill(order, sig, broker=broker)
    assert result.status == OrderStatus.FILLED


@pytest.mark.asyncio
async def test_per_class_override_alone_is_sufficient():
    """Only ``per_asset_class.crypto`` defined (no global default) — crypto
    signal honours that threshold; equity signal falls through to disabled."""
    cfg = {
        "stale_price_gate": {
            "enabled": True,
            "per_asset_class": {"crypto": 75},
        },
        "paper_fee_bps": 0,
        "paper_slippage_bps": 0,
    }
    eng = _engine_with_cfg(cfg)
    # Crypto: 70 bps adverse inside the 75 bps crypto override → filled.
    sig_c = _signal(symbol="BTC-USD", side="sell", suggested_price="100.00")
    sig_c.asset_class = "crypto"
    broker_c = _FakeBroker("99.30")
    result_c = await eng._simulate_fill(
        _order(symbol="BTC-USD", side=OrderSide.SELL), sig_c, broker=broker_c
    )
    assert result_c.status == OrderStatus.FILLED

    # Equity: no global default, no class override → gate disabled, filled.
    sig_e = _signal(symbol="AAPL", side="buy", suggested_price="300.00")
    sig_e.asset_class = "equity"
    broker_e = _FakeBroker("310.00")
    result_e = await eng._simulate_fill(
        _order(symbol="AAPL", side=OrderSide.BUY), sig_e, broker=broker_e
    )
    assert result_e.status == OrderStatus.FILLED
