"""D167.2 — futures whole-contract enforcement at the execution chokepoint.

The signal engine already sizes futures in whole contracts (D165), but a
DOWNSTREAM notional clamp (risk single-name room, crypto venue room) recomputes
``qty = clamped_notional / price`` in RAW units, which can leave a fractional
contract size (observed live: CL=F 313 units = 0.313 of a 1000-bbl contract).

``ExecutionEngine._normalize_order_for_broker`` is the last chokepoint before
fill/submission and now rounds opening futures DOWN to whole contracts (a whole
multiple of the multiplier), skips sub-contract opens (qty -> 0, caught by the
qty<=0 guard), and passes reduce-only closes through unchanged so an existing
fractional residual can still be flattened.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN

import pytest

from brokers.base import Order, OrderSide, OrderType
from execution.engine import ExecutionEngine
from risk.engine import Signal


class _FakeIBKR:
    """Minimal IBKR-like adapter: integer-unit quantization, identity price."""

    broker_name = "ibkr"

    async def quantize_quantity(self, symbol: str, quantity: Decimal) -> Decimal:
        return Decimal(str(quantity)).quantize(Decimal("1"), rounding=ROUND_DOWN)

    async def quantize_price(self, symbol: str, price: Decimal, side: OrderSide) -> Decimal:
        return price


def _fut_order(qty: Decimal, *, price: Decimal | None = None) -> Order:
    return Order(
        symbol="CL=F",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=qty,
        time_in_force="GTC",
        limit_price=price,
    )


def _fut_signal(*, reduce_only: bool = False, side: str = "buy") -> Signal:
    md: dict = {"close": "74.0"}
    if reduce_only:
        md["reduce_only"] = True
    return Signal(
        signal_id="s-fut-1",
        symbol="CL=F",
        side=side,
        strategy="mean_reversion",
        confidence=0.9,
        suggested_quantity=Decimal("313"),
        suggested_price=Decimal("74.0"),
        broker="ibkr",
        asset_class="future",
        timestamp=datetime.now(timezone.utc).isoformat(),
        metadata=md,
    )


@pytest.mark.asyncio
async def test_sub_contract_open_rounded_to_zero() -> None:
    """313 units (0.313 of a 1000-mult contract) -> 0 so the open is skipped."""
    eng = ExecutionEngine(broker_configs={}, paper_mode=True)
    order = _fut_order(Decimal("313"))
    out = await eng._normalize_order_for_broker(order, _fut_signal(), _FakeIBKR())
    assert out.quantity == Decimal("0")


@pytest.mark.asyncio
async def test_fractional_contract_open_floored_to_whole() -> None:
    """2313 units (2.313 contracts) -> 2000 units (2 whole contracts)."""
    eng = ExecutionEngine(broker_configs={}, paper_mode=True)
    order = _fut_order(Decimal("2313"))
    sig = _fut_signal()
    sig.suggested_quantity = Decimal("2313")
    out = await eng._normalize_order_for_broker(order, sig, _FakeIBKR())
    assert out.quantity == Decimal("2000")


@pytest.mark.asyncio
async def test_whole_contract_open_unchanged() -> None:
    eng = ExecutionEngine(broker_configs={}, paper_mode=True)
    order = _fut_order(Decimal("3000"))
    sig = _fut_signal()
    sig.suggested_quantity = Decimal("3000")
    out = await eng._normalize_order_for_broker(order, sig, _FakeIBKR())
    assert out.quantity == Decimal("3000")


@pytest.mark.asyncio
async def test_reduce_only_fractional_close_passes_through() -> None:
    """An existing fractional residual must still be closeable (no flooring)."""
    eng = ExecutionEngine(broker_configs={}, paper_mode=True)
    order = _fut_order(Decimal("313"))
    out = await eng._normalize_order_for_broker(
        order, _fut_signal(reduce_only=True, side="sell"), _FakeIBKR()
    )
    assert out.quantity == Decimal("313")


@pytest.mark.asyncio
async def test_equity_not_contract_rounded() -> None:
    """Control: a normal equity keeps integer-share quantization, not contracts."""
    eng = ExecutionEngine(broker_configs={}, paper_mode=True)
    order = Order(
        symbol="SPY",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=Decimal("313.7"),
        time_in_force="GTC",
    )
    sig = Signal(
        signal_id="s-eq-1",
        symbol="SPY",
        side="buy",
        strategy="momentum_breakout",
        confidence=0.9,
        suggested_quantity=Decimal("313.7"),
        suggested_price=Decimal("100"),
        broker="ibkr",
        asset_class="equity",
        timestamp=datetime.now(timezone.utc).isoformat(),
        metadata={},
    )
    out = await eng._normalize_order_for_broker(order, sig, _FakeIBKR())
    assert out.quantity == Decimal("313")  # integer shares, not whole-contract
