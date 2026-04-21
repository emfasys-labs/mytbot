"""IBKR adapter — forex fractional-qty rounding.

IBKR's IDEALPRO forex desk rejects fractional base-currency quantities
with error 10318 ("This order doesn't support fractional quantity
trading"). Sizing produces fractional units (e.g. a GBP-denominated
notional translated into EURUSD base units), so the adapter rounds DOWN
to whole units before ``placeOrder`` to make sure we never exceed the
sized notional. These tests lock the rounding rule so a future refactor
can't silently re-introduce the cancellation loop.
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest
from ib_insync import Forex, Stock

from brokers.base import Order, OrderSide, OrderType
from brokers.ibkr.adapter import IBKRAdapter


def _make_adapter() -> IBKRAdapter:
    """Real IBKRAdapter, but we'll skip the IB connection path entirely
    by calling the private builder directly. No keys / sockets needed.
    """
    return IBKRAdapter(host="127.0.0.1", port=7497, client_id=1, paper_mode=True)


def _forex_order(qty: Decimal) -> Order:
    return Order(
        symbol="EURUSD",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=qty,
        limit_price=Decimal("1.1762"),
        time_in_force="GTC",
    )


def _equity_order(qty: Decimal) -> Order:
    return Order(
        symbol="AAPL",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=qty,
        time_in_force="GTC",
    )


@pytest.mark.asyncio
async def test_forex_fractional_quantity_rounded_down() -> None:
    adapter = _make_adapter()
    contract = Forex("EURUSD")
    order = _forex_order(Decimal("75409.42964522"))

    with patch.object(
        adapter,
        "_is_paxos_crypto",
        return_value=False,
    ):
        await adapter._build_ib_order_for_contract(order, contract)

    # After the call the Order carries the rounded-down qty so the
    # downstream LimitOrder() gets an integer totalQuantity.
    assert order.quantity == Decimal("75409")


@pytest.mark.asyncio
async def test_forex_whole_quantity_is_untouched() -> None:
    adapter = _make_adapter()
    contract = Forex("EURUSD")
    order = _forex_order(Decimal("75000"))

    with patch.object(adapter, "_is_paxos_crypto", return_value=False):
        await adapter._build_ib_order_for_contract(order, contract)

    assert order.quantity == Decimal("75000")


@pytest.mark.asyncio
async def test_forex_trailing_zero_decimal_is_treated_whole() -> None:
    """``Decimal('75000.00')`` is mathematically whole — must stay as-is."""
    adapter = _make_adapter()
    contract = Forex("EURUSD")
    order = _forex_order(Decimal("75000.00"))

    with patch.object(adapter, "_is_paxos_crypto", return_value=False):
        await adapter._build_ib_order_for_contract(order, contract)

    # Value is whole; we don't force a re-assignment.
    assert Decimal(order.quantity) == Decimal("75000")


@pytest.mark.asyncio
async def test_equity_fractional_quantity_is_preserved() -> None:
    """The forex rounding must NOT apply to stocks — fractional equities
    are legitimate on IBKR Cash-Qty / Alpaca etc."""
    adapter = _make_adapter()
    contract = Stock("AAPL", "SMART", "USD")
    order = _equity_order(Decimal("12.34"))

    with patch.object(adapter, "_is_paxos_crypto", return_value=False):
        await adapter._build_ib_order_for_contract(order, contract)

    assert order.quantity == Decimal("12.34")


@pytest.mark.asyncio
async def test_forex_sub_unit_quantity_not_zeroed() -> None:
    """Qty below 1 unit rounds to 0 — in that case we leave it alone so
    the downstream broker surfaces the real "too small" error rather than
    us silently zeroing a trade."""
    adapter = _make_adapter()
    contract = Forex("EURUSD")
    order = _forex_order(Decimal("0.5"))

    with patch.object(adapter, "_is_paxos_crypto", return_value=False):
        await adapter._build_ib_order_for_contract(order, contract)

    # Original qty preserved (whole > 0 guard prevents 0).
    assert order.quantity == Decimal("0.5")
