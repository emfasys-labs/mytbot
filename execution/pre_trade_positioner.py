from __future__ import annotations

from decimal import Decimal
from typing import Any


class PreTradePositioner:
    """
    Readiness hints: which venues need more cash vs inventory for upcoming arb styles.
    Does not move funds; higher layers (treasury) may act on recommendations.
    """

    def __init__(self, portfolio: Any, config: dict) -> None:
        self._portfolio = portfolio
        self._config = config

    def needs_rebalance(self, symbol: str, venue: str) -> bool:
        threshold = Decimal(str(self._config.get("min_ready_balance", "0")))
        bal = self._get_balance(venue, symbol)
        return bal < threshold

    def _get_balance(self, venue: str, symbol: str) -> Decimal:
        fn = getattr(self._portfolio, "get_balance", None)
        if callable(fn):
            try:
                v = fn(venue, symbol)
                return Decimal(str(v))
            except Exception:  # noqa: BLE001
                pass
        if isinstance(self._portfolio, dict):
            vb = self._portfolio.get("venue_balances", {})
            if isinstance(vb, dict):
                row = vb.get(venue.strip().lower(), {})
                if isinstance(row, dict) and symbol in row:
                    try:
                        return Decimal(str(row[symbol]))
                    except Exception:  # noqa: BLE001
                        pass
        return Decimal("0")

    def recommend_allocation(self, opportunities: list[Any]) -> dict[str, dict[str, str]]:
        allocation: dict[str, dict[str, str]] = {}
        for opp in opportunities:
            sym = getattr(opp, "symbol", None) or opp.get("symbol") if isinstance(opp, dict) else None
            if not sym:
                continue
            allocation.setdefault(str(sym), {})
            buy_v = getattr(opp, "buy_venue", None) or (opp.get("buy_venue") if isinstance(opp, dict) else None)
            sell_v = getattr(opp, "sell_venue", None) or (opp.get("sell_venue") if isinstance(opp, dict) else None)
            if buy_v:
                allocation[str(sym)][str(buy_v)] = "increase_cash"
            if sell_v:
                allocation[str(sym)][str(sell_v)] = "increase_inventory"
        return allocation
