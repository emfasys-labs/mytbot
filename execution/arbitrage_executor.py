from __future__ import annotations

import asyncio
import uuid
from decimal import Decimal
from typing import Any, Optional

from brokers.base import Order, OrderResult, OrderSide, OrderType


class ArbitrageExecutor:
    """Paired long-spot + short-perp submission; flatten on failure when configured."""

    def __init__(self, broker_registry: dict[str, Any], logger: Any | None = None, *, flatten_on_failure: bool = True) -> None:
        self._brokers = broker_registry
        self._logger = logger
        self._flatten_on_failure = flatten_on_failure

    async def open_pair(self, signal: Any, quantity: Decimal) -> dict[str, Any]:
        pair_id = str(uuid.uuid4())
        spot_venue, perp_venue, sym = self._venues(signal)
        spot_broker = self._brokers.get(spot_venue)
        perp_broker = self._brokers.get(perp_venue)

        result: dict[str, Any] = {
            "pair_id": pair_id,
            "status": "pending",
            "spot_order_id": None,
            "perp_order_id": None,
        }

        if spot_broker is None or perp_broker is None:
            result["status"] = "failed"
            result["reason"] = "broker_missing"
            return result

        spot_order = self._build_spot_buy_order(sym, quantity)
        perp_order = self._build_perp_short_order(sym, quantity)

        try:
            spot_task, perp_task = await asyncio.gather(
                spot_broker.place_order(spot_order),
                perp_broker.place_order(perp_order),
                return_exceptions=True,
            )
            if isinstance(spot_task, Exception) and isinstance(perp_task, Exception):
                result["status"] = "failed"
                result["reason"] = "both_failed"
                return result
            if isinstance(spot_task, Exception):
                if isinstance(perp_task, OrderResult) and (perp_task.filled_quantity or Decimal("0")) > 0:
                    await self._try_flatten_perp(perp_broker, perp_task, sym, quantity)
                result["status"] = "failed"
                result["reason"] = "spot_failed"
                return result
            if isinstance(perp_task, Exception):
                if isinstance(spot_task, OrderResult) and (spot_task.filled_quantity or Decimal("0")) > 0:
                    await self._try_flatten_spot(spot_broker, spot_task, sym, quantity)
                result["status"] = "failed"
                result["reason"] = "perp_failed"
                return result

            spot_res = spot_task
            perp_res = perp_task
            result["spot_order_id"] = spot_res.broker_order_id
            result["perp_order_id"] = perp_res.broker_order_id

            ok = await self._poll_both_filled(
                spot_broker,
                perp_broker,
                spot_res.broker_order_id,
                perp_res.broker_order_id,
                sym,
                quantity,
            )
            if not ok:
                result["status"] = "failed"
                result["reason"] = "poll_timeout_or_flattened"
                return result

            result["status"] = "opened"
            return result
        except Exception as exc:  # noqa: BLE001
            if self._logger:
                self._logger.exception("arbitrage_open_failed | pair_id=%s | %s", pair_id, exc)
            if self._flatten_on_failure:
                await self._attempt_emergency_flatten(
                    spot_broker,
                    perp_broker,
                    sym,
                    quantity,
                    result,
                )
            result["status"] = "failed"
            return result

    async def close_pair(self, paired_position: Any) -> dict[str, Any]:
        raise NotImplementedError("close_pair — implement unwind + funding attribution in a follow-up")

    async def _poll_both_filled(
        self,
        spot_broker: Any,
        perp_broker: Any,
        spot_id: str,
        perp_id: str,
        symbol: str,
        quantity: Decimal,
        *,
        timeout_sec: float = 45.0,
        interval_sec: float = 0.5,
    ) -> bool:
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout_sec
        while loop.time() < deadline:
            try:
                sr = await spot_broker.get_order(spot_id)
                pr = await perp_broker.get_order(perp_id)
            except Exception:  # noqa: BLE001
                await asyncio.sleep(interval_sec)
                continue

            def _is_filled(r: OrderResult) -> bool:
                st = getattr(r.status, "value", r.status)
                return str(st).lower() == "filled"

            if _is_filled(sr) and _is_filled(pr):
                return True
            await asyncio.sleep(interval_sec)

        try:
            sr = await spot_broker.get_order(spot_id)
            pr = await perp_broker.get_order(perp_id)
        except Exception:  # noqa: BLE001
            return False
        st_sp = str(getattr(getattr(sr, "status", None), "value", sr.status)).lower()
        st_pp = str(getattr(getattr(pr, "status", None), "value", pr.status)).lower()
        if st_sp == "filled" and st_pp != "filled":
            await self._try_flatten_spot(spot_broker, sr, symbol, quantity)
        elif st_pp == "filled" and st_sp != "filled":
            await self._try_flatten_perp(perp_broker, pr, symbol, quantity)
        return False

    async def _try_flatten_spot(self, broker: Any, result: OrderResult, symbol: str, quantity: Decimal) -> None:
        if not self._flatten_on_failure:
            return
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
        except Exception:  # noqa: BLE001
            pass

    async def _try_flatten_perp(self, broker: Any, result: OrderResult, symbol: str, quantity: Decimal) -> None:
        if not self._flatten_on_failure:
            return
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

    async def _attempt_emergency_flatten(
        self,
        spot_broker: Any,
        perp_broker: Any,
        symbol: str,
        quantity: Decimal,
        result: dict[str, Any],
    ) -> None:
        if result.get("spot_order_id") and not result.get("perp_order_id"):
            try:
                await spot_broker.place_order(
                    Order(
                        symbol=symbol,
                        side=OrderSide.SELL,
                        order_type=OrderType.MARKET,
                        quantity=quantity,
                        client_order_id=str(uuid.uuid4()),
                    )
                )
            except Exception:  # noqa: BLE001
                pass
        if result.get("perp_order_id") and not result.get("spot_order_id"):
            try:
                await perp_broker.place_order(
                    Order(
                        symbol=symbol,
                        side=OrderSide.BUY,
                        order_type=OrderType.MARKET,
                        quantity=quantity,
                        client_order_id=str(uuid.uuid4()),
                    )
                )
            except Exception:  # noqa: BLE001
                pass

    @staticmethod
    def _venues(signal: Any) -> tuple[str, str, str]:
        if isinstance(signal, dict):
            md = signal.get("metadata") or {}
            return (
                str(md.get("spot_venue", signal.get("broker", ""))).strip().lower(),
                str(md.get("perp_venue", "")).strip().lower(),
                str(signal.get("symbol", "")),
            )
        md = getattr(signal, "metadata", None) or {}
        if not isinstance(md, dict):
            md = {}
        return (
            str(md.get("spot_venue", getattr(signal, "broker", ""))).strip().lower(),
            str(md.get("perp_venue", "")).strip().lower(),
            str(getattr(signal, "symbol", "")),
        )

    def _build_spot_buy_order(self, symbol: str, quantity: Decimal) -> Order:
        return Order(
            symbol=symbol,
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            quantity=quantity,
            client_order_id=str(uuid.uuid4()),
        )

    def _build_perp_short_order(self, symbol: str, quantity: Decimal) -> Order:
        return Order(
            symbol=symbol,
            side=OrderSide.SELL,
            order_type=OrderType.MARKET,
            quantity=quantity,
            client_order_id=str(uuid.uuid4()),
        )
