from __future__ import annotations

from decimal import Decimal
from typing import Any, Optional

from brokers.base import OrderBook
from execution.orderbook_analyzer import OrderBookAnalyzer


class ExecutionPlanner:
    """
    Chooses executable size given two books and a max notional cap.
    Used by cross-exchange / latency-aware paths before sending orders.
    """

    def __init__(self, analyzer: OrderBookAnalyzer, config: dict) -> None:
        self._analyzer = analyzer
        self._config = config

    def plan_trade(
        self,
        buy_book: OrderBook,
        sell_book: OrderBook,
        max_notional: Decimal,
    ) -> Optional[dict[str, Any]]:
        fractions = [Decimal(str(x)) for x in self._config.get("size_fractions", [0.25, 0.5, 0.75, 1.0])]
        buy_bids, buy_asks = OrderBookAnalyzer.from_snapshot(buy_book)
        sell_bids, sell_asks = OrderBookAnalyzer.from_snapshot(sell_book)

        best: dict[str, Any] | None = None
        ref_ask = buy_asks[0].price if buy_asks else Decimal("0")
        if ref_ask <= 0:
            return None

        for frac in fractions:
            notional = max_notional * frac
            qty = notional / ref_ask
            if qty <= 0:
                continue

            buy_px = self._analyzer.estimate_market_buy(buy_asks, qty)
            sell_px = self._analyzer.estimate_market_sell(sell_bids, qty)
            if buy_px is None or sell_px is None:
                continue
            edge = sell_px - buy_px
            if edge <= 0:
                continue
            max_slip_bps = Decimal(str(self._config.get("max_slippage_bps", "50")))
            best_ask = buy_asks[0].price if buy_asks else Decimal("0")
            best_bid = sell_bids[0].price if sell_bids else Decimal("0")
            if best_ask > 0:
                slip_buy_bps = abs(buy_px - best_ask) / best_ask * Decimal("10000")
                if slip_buy_bps > max_slip_bps:
                    continue
            if best_bid > 0:
                slip_sell_bps = abs(best_bid - sell_px) / best_bid * Decimal("10000")
                if slip_sell_bps > max_slip_bps:
                    continue
            min_edge_bps = Decimal(str(self._config.get("min_simulated_edge_bps", "0")))
            mid = (buy_px + sell_px) / Decimal("2")
            if mid > 0:
                edge_bps = edge / mid * Decimal("10000")
                if edge_bps < min_edge_bps:
                    continue
            if best is None or edge > best["edge"]:
                best = {
                    "quantity": qty,
                    "buy_price": buy_px,
                    "sell_price": sell_px,
                    "edge": edge,
                    "notional": notional,
                }
        return best
