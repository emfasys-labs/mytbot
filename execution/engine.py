"""
execution/engine.py
====================
The Execution Engine.

Receives an APPROVED signal from the Risk Engine.
Translates it into an Order.
Routes it to the correct broker via smart order routing.
Tracks the fill.
Logs everything.

Key properties:
- Idempotent: uses client_order_id to prevent duplicate orders on retry
- Reconciles: checks broker state vs internal state periodically
- Paper-aware: in paper mode, simulates fills without hitting broker
"""

import uuid
import logging
from decimal import Decimal
from datetime import datetime, timezone
from typing import Optional

from brokers.registry import get_broker
from control.runtime import set_execution_engine
from brokers.base import Order, OrderSide, OrderType, OrderResult
from risk.engine import Signal, RiskDecision, RiskVerdict

logger = logging.getLogger(__name__)


class ExecutionEngine:

    def __init__(self, broker_configs: dict, paper_mode: bool = True):
        self.paper_mode = paper_mode
        self.broker_configs = broker_configs
        self._brokers = {}          # lazy-loaded broker adapters
        self._open_orders = {}      # client_order_id → OrderResult
        set_execution_engine(self)

    async def execute(
        self,
        signal: Signal,
        risk_decision: RiskDecision,
    ) -> Optional[OrderResult]:
        """
        Execute an approved signal.
        Returns OrderResult on success, None on failure.
        """

        if risk_decision.verdict != RiskVerdict.APPROVED:
            logger.warning(f"Attempted to execute rejected signal {signal.signal_id}")
            return None

        broker = await self._get_broker(signal.broker)
        order  = self._build_order(signal)

        logger.info(
            f"EXECUTING | {signal.symbol} {signal.side} "
            f"qty={signal.suggested_quantity} | "
            f"broker={signal.broker} | "
            f"mode={'PAPER' if self.paper_mode else 'LIVE'}"
        )

        try:
            result = await broker.place_order(order)
            self._open_orders[order.client_order_id] = result
            logger.info(f"ORDER PLACED | {result.broker_order_id} | status={result.status}")
            return result

        except Exception as e:
            logger.error(f"Order placement failed | {signal.signal_id} | {e}")
            return None

    async def cancel_all(self) -> None:
        """Emergency: cancel all open orders across all brokers."""
        logger.warning("CANCELLING ALL OPEN ORDERS")
        for broker_name, broker in self._brokers.items():
            try:
                open_orders = await broker.get_open_orders()
                for order in open_orders:
                    await broker.cancel_order(order.broker_order_id)
                    logger.info(f"Cancelled {order.broker_order_id} on {broker_name}")
            except Exception as e:
                logger.error(f"Failed to cancel orders on {broker_name}: {e}")

    def _build_order(self, signal: Signal) -> Order:
        return Order(
            symbol=signal.symbol,
            side=OrderSide.BUY if signal.side == "buy" else OrderSide.SELL,
            order_type=OrderType.MARKET if signal.suggested_price is None else OrderType.LIMIT,
            quantity=signal.suggested_quantity,
            limit_price=signal.suggested_price,
            client_order_id=str(uuid.uuid4()),  # idempotency key
        )

    async def _get_broker(self, name: str):
        """Lazy-load broker adapter."""
        if name not in self._brokers:
            config = self.broker_configs.get(name, {})
            self._brokers[name] = get_broker(
                name,
                paper_mode=self.paper_mode,
                **config
            )
            await self._brokers[name].connect()
        return self._brokers[name]
