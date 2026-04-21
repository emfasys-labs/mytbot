"""Marketable-limit price rewriting in ExecutionEngine.

When the allocator emits a LIMIT BUY at the last 1h-bar close (e.g. £58.24)
but the current ask is £58.31, the order sits unfilled forever. The engine
rewrites the limit to ``ask × (1 + slip)`` so it crosses the spread on arrival.
These tests lock that behaviour down.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import pytest

from brokers.base import Order, OrderBook, OrderSide, OrderType
from control.runtime import set_risk_engine
from execution.engine import ExecutionEngine
from risk.engine import Signal


@dataclass
class _FakeRiskEngine:
    config: dict = field(default_factory=dict)


@dataclass
class _FakeBroker:
    bid: Decimal | None = Decimal("58.24")
    ask: Decimal | None = Decimal("58.31")
    last: Decimal | None = None
    raise_book: bool = False
    raise_last: bool = False

    async def get_order_book(self, symbol: str, depth: int = 1) -> OrderBook:
        if self.raise_book:
            raise RuntimeError("book timeout")
        bids = [(self.bid, Decimal("1000"))] if self.bid else []
        asks = [(self.ask, Decimal("1000"))] if self.ask else []
        return OrderBook(symbol=symbol, timestamp="", bids=bids, asks=asks)

    async def get_last_price(self, symbol: str) -> Decimal:
        if self.raise_last:
            raise RuntimeError("last timeout")
        return self.last if self.last is not None else Decimal("0")


def _engine(**env) -> ExecutionEngine:
    set_risk_engine(_FakeRiskEngine())
    return ExecutionEngine(broker_configs={}, paper_mode=True)


def _order(side: OrderSide = OrderSide.BUY, limit: str = "58.24") -> Order:
    return Order(
        symbol="FUTY",
        side=side,
        order_type=OrderType.LIMIT,
        quantity=Decimal("100"),
        limit_price=Decimal(limit),
        client_order_id="test-1",
    )


def _signal(symbol: str = "FUTY", broker: str = "ibkr", side: str = "buy") -> Signal:
    return Signal(
        signal_id="s1",
        symbol=symbol,
        side=side,
        strategy="mean_reversion",
        confidence=0.7,
        suggested_quantity=Decimal("100"),
        suggested_price=Decimal("58.24"),
        broker=broker,
        asset_class="equity",
        timestamp="2026-04-21T00:00:00+00:00",
        metadata={},
    )


@pytest.mark.asyncio
async def test_buy_limit_rewritten_to_ask_plus_slip(monkeypatch):
    monkeypatch.setenv("EXECUTION_MARKETABLE_SLIP_BPS", "10")  # 0.10%
    engine = _engine()
    broker = _FakeBroker(bid=Decimal("58.24"), ask=Decimal("58.30"))

    out = await engine._apply_marketable_limit(_order(), _signal(), broker)

    # Expected: 58.30 * (1 + 0.001) = 58.3583
    assert out.limit_price is not None
    assert out.limit_price > Decimal("58.30")
    assert out.limit_price < Decimal("58.40")
    assert engine.marketable_adjusted == 1


@pytest.mark.asyncio
async def test_sell_limit_rewritten_to_bid_minus_slip(monkeypatch):
    monkeypatch.setenv("EXECUTION_MARKETABLE_SLIP_BPS", "10")
    engine = _engine()
    broker = _FakeBroker(bid=Decimal("100.00"), ask=Decimal("100.10"))

    out = await engine._apply_marketable_limit(
        _order(side=OrderSide.SELL, limit="100.10"), _signal(side="sell"), broker
    )

    # Expected: 100.00 * (1 - 0.001) = 99.90
    assert out.limit_price is not None
    assert out.limit_price < Decimal("100.00")
    assert out.limit_price > Decimal("99.80")


@pytest.mark.asyncio
async def test_disabled_when_slip_zero(monkeypatch):
    monkeypatch.setenv("EXECUTION_MARKETABLE_SLIP_BPS", "0")
    engine = _engine()
    broker = _FakeBroker()

    original = _order()
    out = await engine._apply_marketable_limit(original, _signal(), broker)
    assert out.limit_price == original.limit_price
    assert engine.marketable_adjusted == 0


@pytest.mark.asyncio
async def test_skips_market_orders(monkeypatch):
    monkeypatch.setenv("EXECUTION_MARKETABLE_SLIP_BPS", "10")
    engine = _engine()
    broker = _FakeBroker()

    mkt = Order(
        symbol="FUTY",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=Decimal("100"),
        limit_price=None,
        client_order_id="m1",
    )
    out = await engine._apply_marketable_limit(mkt, _signal(), broker)
    assert out is mkt
    assert engine.marketable_adjusted == 0


@pytest.mark.asyncio
async def test_falls_back_to_last_price_when_book_empty(monkeypatch):
    monkeypatch.setenv("EXECUTION_MARKETABLE_SLIP_BPS", "10")
    engine = _engine()
    broker = _FakeBroker(bid=None, ask=None, last=Decimal("58.40"))

    out = await engine._apply_marketable_limit(_order(), _signal(), broker)
    assert out.limit_price is not None
    assert out.limit_price > Decimal("58.40")  # buy bumped up via slip
    assert engine.marketable_adjusted == 1


@pytest.mark.asyncio
async def test_preserves_order_when_no_reference(monkeypatch):
    monkeypatch.setenv("EXECUTION_MARKETABLE_SLIP_BPS", "10")
    engine = _engine()
    broker = _FakeBroker(bid=None, ask=None, last=Decimal("0"))

    original = _order()
    out = await engine._apply_marketable_limit(original, _signal(), broker)
    assert out.limit_price == original.limit_price
    assert engine.marketable_adjusted == 0


@pytest.mark.asyncio
async def test_tolerates_book_exception(monkeypatch):
    monkeypatch.setenv("EXECUTION_MARKETABLE_SLIP_BPS", "10")
    engine = _engine()
    broker = _FakeBroker(raise_book=True, last=Decimal("58.30"))

    out = await engine._apply_marketable_limit(_order(), _signal(), broker)
    # Book failed → should fall through to last-price path.
    assert out.limit_price is not None
    assert out.limit_price > Decimal("58.30")
    assert engine.marketable_adjusted == 1


@pytest.mark.asyncio
async def test_no_broker_returns_order_unchanged(monkeypatch):
    monkeypatch.setenv("EXECUTION_MARKETABLE_SLIP_BPS", "10")
    engine = _engine()

    original = _order()
    out = await engine._apply_marketable_limit(original, _signal(), None)
    assert out is original
    assert engine.marketable_adjusted == 0
