"""Unit tests for order-book imbalance microstructure helper."""

from decimal import Decimal

from execution.orderbook_analyzer import OrderBookLevel
from signals.microstructure.imbalance_detector import ImbalanceDetector


def _lvl(price: str, qty: str) -> OrderBookLevel:
    return OrderBookLevel(price=Decimal(price), quantity=Decimal(qty))


def test_compute_imbalance_symmetric_book_is_zero() -> None:
    bids = [_lvl("100", "10"), _lvl("99", "10")]
    asks = [_lvl("101", "10"), _lvl("102", "10")]
    imb = ImbalanceDetector.compute_imbalance(bids, asks, depth=2)
    assert imb == Decimal("0")


def test_compute_imbalance_bid_heavy_positive() -> None:
    bids = [_lvl("100", "30"), _lvl("99", "10")]
    asks = [_lvl("101", "5"), _lvl("102", "5")]
    imb = ImbalanceDetector.compute_imbalance(bids, asks, depth=2)
    assert imb > 0
    # (40 - 10) / 50 = 0.6
    assert abs(imb - Decimal("0.6")) < Decimal("0.0001")


def test_compute_imbalance_ask_heavy_negative() -> None:
    bids = [_lvl("100", "5")]
    asks = [_lvl("101", "25")]
    imb = ImbalanceDetector.compute_imbalance(bids, asks, depth=5)
    assert imb < 0


def test_compute_imbalance_zero_total_returns_zero() -> None:
    bids: list[OrderBookLevel] = []
    asks: list[OrderBookLevel] = []
    assert ImbalanceDetector.compute_imbalance(bids, asks) == Decimal("0")
