from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from typing import Any, List

from portfolio.capital_scheduler import CapitalScheduler
from portfolio.strategy_opportunity import StrategyOpportunity, compute_priority_score


class StrategyCoordinator:
    """
    Ranks normalised opportunities and proposes capital via ``CapitalScheduler``.
    Does not place orders or bypass the risk engine.
    """

    def __init__(self, regime_detector: Any, scheduler: CapitalScheduler, logger: Any | None = None) -> None:
        self._regime_detector = regime_detector
        self._scheduler = scheduler
        self._logger = logger

    def coordinate(
        self,
        opportunities: List[StrategyOpportunity],
        portfolio_state: dict,
    ) -> list[tuple[StrategyOpportunity, Decimal]]:
        regime = None
        if self._regime_detector is not None and hasattr(self._regime_detector, "current_regime"):
            try:
                regime = self._regime_detector.current_regime()
            except Exception:  # noqa: BLE001
                regime = None

        rescored: list[StrategyOpportunity] = []
        for o in opportunities:
            rescored.append(self._apply_regime_adjustment(o, regime))

        ranked = sorted(rescored, key=lambda x: x.priority_score, reverse=True)
        return self._scheduler.allocate(ranked, portfolio_state)

    def _apply_regime_adjustment(self, opportunity: StrategyOpportunity, regime: Any) -> StrategyOpportunity:
        mult = Decimal("1")
        if regime is None:
            return opportunity
        rm = getattr(regime, "strategy_multipliers", None)
        if isinstance(rm, dict):
            try:
                mult = Decimal(str(rm.get(opportunity.strategy_name, "1")))
            except Exception:  # noqa: BLE001
                mult = Decimal("1")
        if mult == Decimal("1"):
            return opportunity
        new_priority = compute_priority_score(
            opportunity.expected_edge * mult,
            opportunity.confidence,
            opportunity.regime_fit_score,
            opportunity.execution_score,
            opportunity.risk_cost_score,
        )
        return replace(opportunity, priority_score=new_priority)
