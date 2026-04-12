from __future__ import annotations

from decimal import Decimal

from execution.orderbook_analyzer import OrderBookLevel


class LiquidityTracker:
    def __init__(self) -> None:
        self._previous: dict[str, tuple[Decimal, Decimal]] = {}

    def detect_disappearing_liquidity(
        self,
        symbol: str,
        bid_levels: list[OrderBookLevel],
        ask_levels: list[OrderBookLevel],
        depth: int = 5,
    ) -> bool:
        bid_volume = sum(level.quantity for level in bid_levels[:depth])
        key = symbol.strip().upper()
        prev = self._previous.get(key)
        self._previous[key] = (bid_volume, sum(level.quantity for level in ask_levels[:depth]))
        if prev is None:
            return False
        prev_bid, _ = prev
        if prev_bid <= 0:
            return False
        return bid_volume / prev_bid < Decimal("0.5")
