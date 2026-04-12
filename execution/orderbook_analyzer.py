from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import List, Optional, Sequence

from brokers.base import OrderBook


@dataclass
class OrderBookLevel:
    price: Decimal
    quantity: Decimal


def levels_from_orderbook(
    bids: Sequence[Tuple[Decimal, Decimal]],
    asks: Sequence[Tuple[Decimal, Decimal]],
) -> tuple[list[OrderBookLevel], list[OrderBookLevel]]:
    bid_levels = [OrderBookLevel(price=p, quantity=q) for p, q in bids if q > 0]
    ask_levels = [OrderBookLevel(price=p, quantity=q) for p, q in asks if q > 0]
    return bid_levels, ask_levels


class OrderBookAnalyzer:
    """Walk the book to estimate average fill prices (pre-trade slippage simulation)."""

    @staticmethod
    def estimate_market_buy(asks: List[OrderBookLevel], quantity: Decimal) -> Optional[Decimal]:
        remaining = quantity
        total_cost = Decimal("0")
        for level in asks:
            if remaining <= 0:
                break
            fill_qty = min(remaining, level.quantity)
            total_cost += fill_qty * level.price
            remaining -= fill_qty
        if remaining > 0:
            return None
        if quantity <= 0:
            return None
        return total_cost / quantity

    @staticmethod
    def estimate_market_sell(bids: List[OrderBookLevel], quantity: Decimal) -> Optional[Decimal]:
        remaining = quantity
        total_value = Decimal("0")
        for level in bids:
            if remaining <= 0:
                break
            fill_qty = min(remaining, level.quantity)
            total_value += fill_qty * level.price
            remaining -= fill_qty
        if remaining > 0:
            return None
        if quantity <= 0:
            return None
        return total_value / quantity

    @classmethod
    def from_snapshot(cls, book: OrderBook, depth: int = 25) -> tuple[list[OrderBookLevel], list[OrderBookLevel]]:
        bids = book.bids[:depth]
        asks = book.asks[:depth]
        return levels_from_orderbook(bids, asks)


def notional_to_base_quantity(notional: Decimal, reference_price: Decimal) -> Decimal:
    if reference_price <= 0:
        return Decimal("0")
    return notional / reference_price
