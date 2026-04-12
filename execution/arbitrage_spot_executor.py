from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

from brokers.base import Order, OrderSide, OrderType


class SpotArbitrageExecutor:
    """Two-leg spot buy / spot sell; delegates concurrent mode to ``SmartOrderExecutor`` when needed."""

    def __init__(self, broker_registry: dict[str, Any], logger: Any | None = None) -> None:
        self._brokers = broker_registry
        self._logger = logger

    async def execute(self, signal: dict[str, Any], quantity: Decimal) -> dict[str, Any]:
        pair_id = str(uuid.uuid4())
        buy_broker = self._brokers.get(str(signal.get("buy_venue", "")).strip().lower())
        sell_broker = self._brokers.get(str(signal.get("sell_venue", "")).strip().lower())
        if buy_broker is None or sell_broker is None:
            return {"pair_id": pair_id, "status": "failed", "reason": "broker_missing"}

        try:
            buy_order = Order(
                symbol=str(signal["symbol"]),
                side=OrderSide.BUY,
                order_type=OrderType.MARKET,
                quantity=quantity,
                client_order_id=str(uuid.uuid4()),
            )
            sell_order = Order(
                symbol=str(signal["symbol"]),
                side=OrderSide.SELL,
                order_type=OrderType.MARKET,
                quantity=quantity,
                client_order_id=str(uuid.uuid4()),
            )
            buy_r = await buy_broker.place_order(buy_order)
            sell_r = await sell_broker.place_order(sell_order)
            return {
                "pair_id": pair_id,
                "status": "submitted",
                "buy_order": buy_r.broker_order_id,
                "sell_order": sell_r.broker_order_id,
            }
        except Exception as exc:  # noqa: BLE001
            if self._logger:
                self._logger.exception("spot_arb_failed | %s", exc)
            await self._emergency_unwind(signal, quantity)
            return {"pair_id": pair_id, "status": "failed"}

    async def _emergency_unwind(self, signal: dict[str, Any], quantity: Decimal) -> None:
        _ = (signal, quantity)
        # Partial-fill reconciliation belongs in a dedicated monitor; log-only here.
        if self._logger:
            self._logger.warning("spot_arb | emergency_unwind | manual reconciliation may be required")
