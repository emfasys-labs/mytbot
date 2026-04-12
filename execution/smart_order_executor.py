from __future__ import annotations

import asyncio
import uuid
from decimal import Decimal
from typing import Any, Optional

from brokers.base import Order, OrderResult, OrderSide, OrderType


class SmartOrderExecutor:
    """Concurrent dual-leg spot arb submission with basic failure handling."""

    def __init__(self, broker_registry: dict[str, Any], latency_optimizer: Any, logger: Any | None = None) -> None:
        self._brokers = broker_registry
        self._latency = latency_optimizer
        self._logger = logger

    async def execute_spot_arbitrage(
        self,
        signal: dict[str, Any],
        quantity: Decimal,
        *,
        max_latency_ms: float = 500.0,
    ) -> dict[str, Any]:
        pair_id = str(uuid.uuid4())
        buy_name = str(signal.get("buy_venue", "")).strip().lower()
        sell_name = str(signal.get("sell_venue", "")).strip().lower()
        buy_broker = self._brokers.get(buy_name)
        sell_broker = self._brokers.get(sell_name)
        if buy_broker is None or sell_broker is None:
            return self._reject(pair_id, "broker_missing")

        if self._latency.is_too_slow(buy_name, max_latency_ms):
            return self._reject(pair_id, "buy_latency_too_high")
        if self._latency.is_too_slow(sell_name, max_latency_ms):
            return self._reject(pair_id, "sell_latency_too_high")

        symbol = str(signal.get("symbol", ""))
        buy_order = self._build_buy_order(signal, quantity)
        sell_order = self._build_sell_order(signal, quantity)

        try:
            results = await asyncio.gather(
                buy_broker.place_order(buy_order),
                sell_broker.place_order(sell_order),
                return_exceptions=True,
            )
            buy_result, sell_result = results[0], results[1]

            if isinstance(buy_result, Exception) and isinstance(sell_result, Exception):
                return self._reject(pair_id, "both_failed")
            if isinstance(buy_result, Exception):
                if isinstance(sell_result, OrderResult) and sell_result.filled_quantity > 0:
                    await self._try_flatten_sell(sell_broker, sell_result, symbol, quantity)
                return self._reject(pair_id, "buy_failed")
            if isinstance(sell_result, Exception):
                if isinstance(buy_result, OrderResult) and buy_result.filled_quantity > 0:
                    await self._try_flatten_buy(buy_broker, buy_result, symbol, quantity)
                return self._reject(pair_id, "sell_failed")

            br = buy_result
            sr = sell_result
            ok = await self._poll_both_filled(
                buy_broker,
                sell_broker,
                br.broker_order_id,
                sr.broker_order_id,
                symbol,
                quantity,
            )
            if not ok:
                return self._reject(pair_id, "poll_timeout_or_flattened")
            return {
                "pair_id": pair_id,
                "status": "submitted",
                "buy_order_id": br.broker_order_id,
                "sell_order_id": sr.broker_order_id,
            }
        except Exception as exc:  # noqa: BLE001
            if self._logger:
                self._logger.exception("smart_order_executor | %s", exc)
            return self._reject(pair_id, "unexpected_error")

    def _reject(self, pair_id: str, reason: str) -> dict[str, Any]:
        return {"pair_id": pair_id, "status": "rejected", "reason": reason}

    async def _poll_both_filled(
        self,
        buy_broker: Any,
        sell_broker: Any,
        buy_id: str,
        sell_id: str,
        symbol: str,
        quantity: Decimal,
        *,
        timeout_sec: float = 45.0,
        interval_sec: float = 0.5,
    ) -> bool:
        """Wait for both legs filled; on timeout flatten any partial."""
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout_sec
        while loop.time() < deadline:
            try:
                br = await buy_broker.get_order(buy_id)
                sr = await sell_broker.get_order(sell_id)
            except Exception:  # noqa: BLE001
                await asyncio.sleep(interval_sec)
                continue

            def _is_filled(r: OrderResult) -> bool:
                st = getattr(r.status, "value", r.status)
                return str(st).lower() == "filled"

            bf = _is_filled(br)
            sf = _is_filled(sr)
            if bf and sf:
                return True
            await asyncio.sleep(interval_sec)

        try:
            br = await buy_broker.get_order(buy_id)
            sr = await sell_broker.get_order(sell_id)
        except Exception:  # noqa: BLE001
            return False
        st_b = str(getattr(getattr(br, "status", None), "value", br.status)).lower()
        st_s = str(getattr(getattr(sr, "status", None), "value", sr.status)).lower()
        if st_b == "filled" and st_s != "filled":
            await self._try_flatten_buy(buy_broker, br, symbol, quantity)
        elif st_s == "filled" and st_b != "filled":
            await self._try_flatten_sell(sell_broker, sr, symbol, quantity)
        return False

    def _build_buy_order(self, signal: dict[str, Any], quantity: Decimal) -> Order:
        md = signal.get("metadata") or {}
        ask = md.get("buy_limit_from_ask")
        limit: Optional[Decimal] = None
        if ask is not None:
            try:
                a = Decimal(str(ask))
                bump = Decimal(str(signal.get("buy_limit_bump_pct", "0.001")))
                limit = a * (Decimal("1") + bump)
            except Exception:  # noqa: BLE001
                limit = None
        return Order(
            symbol=str(signal.get("symbol", "")),
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT if limit is not None else OrderType.MARKET,
            quantity=quantity,
            limit_price=limit,
            client_order_id=str(uuid.uuid4()),
        )

    def _build_sell_order(self, signal: dict[str, Any], quantity: Decimal) -> Order:
        md = signal.get("metadata") or {}
        bid = md.get("sell_limit_from_bid")
        limit: Optional[Decimal] = None
        if bid is not None:
            try:
                b = Decimal(str(bid))
                bump = Decimal(str(signal.get("sell_limit_bump_pct", "0.001")))
                limit = b * (Decimal("1") - bump)
            except Exception:  # noqa: BLE001
                limit = None
        return Order(
            symbol=str(signal.get("symbol", "")),
            side=OrderSide.SELL,
            order_type=OrderType.LIMIT if limit is not None else OrderType.MARKET,
            quantity=quantity,
            limit_price=limit,
            client_order_id=str(uuid.uuid4()),
        )

    async def _try_flatten_sell(self, broker: Any, result: OrderResult, symbol: str, quantity: Decimal) -> None:
        try:
            await broker.place_order(
                Order(
                    symbol=symbol,
                    side=OrderSide.BUY,
                    order_type=OrderType.MARKET,
                    quantity=result.filled_quantity or quantity,
                    client_order_id=str(uuid.uuid4()),
                )
            )
        except Exception:  # noqa: BLE001
            pass

    async def _try_flatten_buy(self, broker: Any, result: OrderResult, symbol: str, quantity: Decimal) -> None:
        try:
            await broker.place_order(
                Order(
                    symbol=symbol,
                    side=OrderSide.SELL,
                    order_type=OrderType.MARKET,
                    quantity=result.filled_quantity or quantity,
                    client_order_id=str(uuid.uuid4()),
                )
            )
        except Exception:  # noqa:BLE001
            pass
