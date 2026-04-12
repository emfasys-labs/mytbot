"""Unit tests for IBKR order-status mapping edge cases (PAXOS / NaN remaining)."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest

from brokers.base import OrderStatus
from brokers.ibkr.adapter import IBKRAdapter


def _mock_trade(
    *,
    total_qty: Decimal | int | float,
    status: str,
    os_filled: Decimal | int | float,
    remaining,
    fills: tuple | list = (),
) -> SimpleNamespace:
    order = SimpleNamespace(
        totalQuantity=total_qty,
        permId=0,
        orderId=1,
        orderRef=None,
        action="BUY",
    )
    order_status = SimpleNamespace(
        status=status,
        filled=os_filled,
        remaining=remaining,
        avgFillPrice=100.0,
    )
    return SimpleNamespace(
        order=order,
        orderStatus=order_status,
        fills=list(fills),
        contract=SimpleNamespace(),
    )


@pytest.fixture
def adapter() -> IBKRAdapter:
    return IBKRAdapter.__new__(IBKRAdapter)


def test_remaining_safe_treats_nan_as_zero(adapter: IBKRAdapter) -> None:
    t = _mock_trade(total_qty=0, status="Submitted", os_filled=0, remaining=float("nan"))
    assert adapter._remaining_safe(t) == 0.0


def test_paxos_zero_qty_filled_when_remaining_nan_and_exec_shares(adapter: IBKRAdapter) -> None:
    ex = SimpleNamespace(shares=Decimal("0.01"))
    fill = SimpleNamespace(execution=ex, commissionReport=None)
    t = _mock_trade(
        total_qty=0,
        status="Submitted",
        os_filled=0,
        remaining=float("nan"),
        fills=(fill,),
    )
    assert adapter._map_ib_status(t) == OrderStatus.FILLED


def test_paxos_partial_when_remaining_positive(adapter: IBKRAdapter) -> None:
    ex = SimpleNamespace(shares=Decimal("0.01"))
    fill = SimpleNamespace(execution=ex, commissionReport=None)
    t = _mock_trade(
        total_qty=0,
        status="Submitted",
        os_filled=0,
        remaining=0.5,
        fills=(fill,),
    )
    assert adapter._map_ib_status(t) == OrderStatus.PARTIALLY_FILLED


def test_trade_ib_is_terminal_paxos_nan_remaining(adapter: IBKRAdapter) -> None:
    ex = SimpleNamespace(shares=Decimal("0.01"))
    fill = SimpleNamespace(execution=ex, commissionReport=None)
    t = _mock_trade(
        total_qty=0,
        status="Submitted",
        os_filled=0,
        remaining=float("nan"),
        fills=(fill,),
    )
    assert adapter._trade_ib_is_terminal(t) is True
