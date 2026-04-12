from __future__ import annotations

from decimal import Decimal
from typing import Sequence

from execution.orderbook_analyzer import OrderBookLevel


class ImbalanceDetector:
    @staticmethod
    def compute_imbalance(
        bids: Sequence[OrderBookLevel],
        asks: Sequence[OrderBookLevel],
        depth: int = 5,
    ) -> Decimal:
        bid_volume = sum(level.quantity for level in bids[:depth])
        ask_volume = sum(level.quantity for level in asks[:depth])
        tot = bid_volume + ask_volume
        if tot <= 0:
            return Decimal("0")
        return (bid_volume - ask_volume) / tot
