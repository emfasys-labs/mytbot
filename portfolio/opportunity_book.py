from __future__ import annotations

from typing import List

from portfolio.strategy_opportunity import StrategyOpportunity


class OpportunityBook:
    def __init__(self) -> None:
        self._opportunities: List[StrategyOpportunity] = []

    def add(self, opportunity: StrategyOpportunity) -> None:
        self._opportunities.append(opportunity)

    def clear(self) -> None:
        self._opportunities.clear()

    def ranked(self) -> list[StrategyOpportunity]:
        return sorted(self._opportunities, key=lambda x: x.priority_score, reverse=True)

    def __len__(self) -> int:
        return len(self._opportunities)
