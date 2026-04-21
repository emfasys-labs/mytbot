"""Snapshot readiness helper for broker dashboard badges."""

from decimal import Decimal

from brokers.base import Balance
from system.broker_manager import (
    BrokerManager,
    _balance_poll_mean_ready,
    _balance_rows_mean_ready,
)


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


def test_balance_poll_mean_ready_ibkr_still_requires_rows() -> None:
    assert _balance_poll_mean_ready("ibkr", []) is False


def test_balance_poll_mean_ready_non_ibkr_allows_empty_wallet() -> None:
    assert _balance_poll_mean_ready("bybit", []) is True
    assert _balance_poll_mean_ready("kraken", []) is True


def test_kraken_backoff_is_shorter_than_default() -> None:
    mgr = BrokerManager(paper_mode=True)
    mgr._broker_fail_count["kraken"] = 1
    mgr._broker_fail_count["binance"] = 1
    assert mgr._broker_backoff("kraken") < mgr._broker_backoff("binance")


def test_binance_and_bybit_backoff_are_shorter_than_default() -> None:
    mgr = BrokerManager(paper_mode=True)
    mgr._broker_fail_count["binance"] = 1
    mgr._broker_fail_count["bybit"] = 1
    mgr._broker_fail_count["alpaca"] = 1
    assert mgr._broker_backoff("binance") < mgr._broker_backoff("alpaca")
    assert mgr._broker_backoff("bybit") < mgr._broker_backoff("alpaca")


def test_connect_timeout_scales_for_binance_and_bybit() -> None:
    mgr = BrokerManager(paper_mode=True)
    mgr._broker_fail_count["bybit"] = 2
    mgr._broker_fail_count["binance"] = 2
    mgr._broker_fail_count["alpaca"] = 2
    assert mgr._connect_timeout("bybit") > mgr._connect_timeout("alpaca")
    assert mgr._connect_timeout("binance") > mgr._connect_timeout("alpaca")
