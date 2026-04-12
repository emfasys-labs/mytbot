from __future__ import annotations

from decimal import Decimal
from typing import Any


class CrossExchangeRiskChecks:
    def __init__(self, config: dict) -> None:
        self._config = config

    def validate(self, signal: dict, portfolio: dict, venues: Any) -> tuple[bool, str]:
        if not self._config.get("enabled", True):
            return (True, "cross_arb_checks_disabled")

        meta = signal.get("metadata") or {}
        try:
            net_spread = Decimal(str(meta.get("net_spread", "0")))
        except Exception:  # noqa: BLE001
            net_spread = Decimal("0")
        if net_spread <= 0:
            return (False, "no_profit")

        buy_v = str(signal.get("buy_venue", "")).strip().lower()
        sell_v = str(signal.get("sell_venue", "")).strip().lower()

        if not self._has_balance(portfolio, buy_v, cash_key=True):
            return (False, "no_balance_buy_side")
        if not self._has_inventory(portfolio, sell_v, signal.get("symbol", "")):
            return (False, "no_inventory_sell_side")

        is_unhealthy = getattr(venues, "is_unhealthy", None)
        if callable(is_unhealthy):
            if is_unhealthy(buy_v):
                return (False, "buy_venue_unhealthy")
            if is_unhealthy(sell_v):
                return (False, "sell_venue_unhealthy")

        return (True, "approved")

    @staticmethod
    def _has_balance(portfolio: dict, venue: str, *, cash_key: bool) -> bool:
        if not venue:
            return True
        strict = portfolio.get("arbitrage_require_prefunded", False) is True
        if not strict:
            return True
        vb = portfolio.get("venue_balances", {})
        if not isinstance(vb, dict):
            return False
        row = vb.get(venue, {})
        if not isinstance(row, dict):
            return False
        if cash_key:
            return Decimal(str(row.get("USDT", row.get("USD", "0")))) > 0
        return True

    @staticmethod
    def _has_inventory(portfolio: dict, venue: str, symbol: str) -> bool:
        if not venue:
            return True
        strict = portfolio.get("arbitrage_require_prefunded", False) is True
        if not strict:
            return True
        inv = portfolio.get("venue_inventory", {})
        if not isinstance(inv, dict):
            return False
        row = inv.get(venue, {})
        if not isinstance(row, dict):
            return False
        base = str(symbol or "").replace("USDT", "").replace("-", "")
        qty = row.get(symbol) or row.get(base)
        if qty is None:
            return False
        try:
            return Decimal(str(qty)) > 0
        except Exception:  # noqa: BLE001
            return False
