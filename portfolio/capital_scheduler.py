from __future__ import annotations

from decimal import Decimal
from typing import Any, List, Tuple

from portfolio.strategy_opportunity import StrategyOpportunity


class CapitalScheduler:
    def __init__(self, config: dict, logger: Any | None = None) -> None:
        self._config = config
        self._logger = logger

    def allocate(
        self,
        ranked_opportunities: List[StrategyOpportunity],
        portfolio_state: dict,
    ) -> list[Tuple[StrategyOpportunity, Decimal]]:
        allocations: list[Tuple[StrategyOpportunity, Decimal]] = []
        try:
            free_capital = Decimal(str(portfolio_state.get("free_capital", portfolio_state.get("tradable_capital", "0"))))
        except Exception:  # noqa: BLE001
            free_capital = Decimal("0")

        reserve_pct = Decimal(str(self._config.get("arbitrage_reserve_pct", "0")))
        if reserve_pct > 0 and free_capital > 0:
            free_capital = free_capital * (Decimal("1") - min(reserve_pct, Decimal("0.5")))

        for opp in ranked_opportunities:
            if free_capital <= 0:
                break
            req = opp.capital_required
            if req <= 0:
                continue
            allocation = min(req, free_capital)
            if allocation <= 0:
                continue
            allocations.append((opp, allocation))
            free_capital -= allocation
        return allocations
