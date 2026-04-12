from __future__ import annotations

from decimal import Decimal
from typing import Dict, List


class OrderFlowAnalyzer:
    def __init__(self) -> None:
        self._history: Dict[str, List[Decimal]] = {}

    def record(self, symbol: str, imbalance: Decimal) -> None:
        self._history.setdefault(symbol, []).append(imbalance)
        if len(self._history[symbol]) > 200:
            self._history[symbol] = self._history[symbol][-200:]

    def momentum(self, symbol: str) -> Decimal:
        values = self._history.get(symbol, [])
        if len(values) < 3:
            return Decimal("0")
        return values[-1] - values[-3]
