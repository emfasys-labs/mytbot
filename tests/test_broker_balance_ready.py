"""Snapshot readiness helper for broker dashboard badges."""

from decimal import Decimal

from brokers.base import Balance
from system.broker_manager import _balance_rows_mean_ready


def test_balance_rows_mean_ready_empty_not_ready() -> None:
    assert _balance_rows_mean_ready([]) is False


def test_balance_rows_mean_ready_nonempty_currency_ok_even_zero_total() -> None:
    row = Balance(
        currency="USD",
        total=Decimal("0"),
        available=Decimal("0"),
        reserved=Decimal("0"),
    )
    assert _balance_rows_mean_ready([row]) is True


def test_balance_rows_mean_ready_no_currency_ignored() -> None:
    row = Balance(
        currency="",
        total=Decimal("1"),
        available=Decimal("1"),
        reserved=Decimal("0"),
    )
    assert _balance_rows_mean_ready([row]) is False
