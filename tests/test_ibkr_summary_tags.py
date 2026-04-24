"""Tag precedence for IBKR :meth:`IBKRAdapter.get_balance` per-currency totals."""

from __future__ import annotations

from decimal import Decimal

from brokers.ibkr.adapter import _total_from_account_summary_tags


def test_prefers_net_liquidation_over_cash_for_usd_only_account() -> None:
    """Single-currency USD: all tags on one row; NetLiq is full NAV, cash is only part."""
    tags = {
        "CashBalance": Decimal("884000"),
        "TotalCashValue": Decimal("884000"),
        "NetLiquidation": Decimal("1055000"),
    }
    assert _total_from_account_summary_tags(tags) == Decimal("1055000")


def test_falls_back_when_no_net_liquidation() -> None:
    tags = {
        "TotalCashValue": Decimal("50000"),
        "CashBalance": Decimal("48000"),
    }
    assert _total_from_account_summary_tags(tags) == Decimal("50000")


def test_falls_back_to_cash_only() -> None:
    tags = {"CashBalance": Decimal("100")}
    assert _total_from_account_summary_tags(tags) == Decimal("100")
