"""D165 — IBKR adapter futures boundary conversions.

The internal ledger keeps futures quantity in notional-consistent UNITS
(contracts * multiplier). At the broker boundary the adapter must:
  * convert units -> whole CONTRACTS when building the IB order,
  * convert CONTRACTS -> units when reading fills/positions back,
  * key the symbol as the canonical ``ROOT=F`` form.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from ib_insync import Future

from brokers.base import Order, OrderSide, OrderType
from brokers.ibkr.adapter import IBKRAdapter


def _adapter() -> IBKRAdapter:
    return IBKRAdapter(host="127.0.0.1", port=7497, client_id=1, paper_mode=True)


def _fut_contract(root: str = "CL", mult: str = "1000", exch: str = "NYMEX") -> Future:
    c = Future(root, "202607", exch, multiplier=mult, currency="USD")
    return c


def _fut_order(qty: Decimal) -> Order:
    return Order(
        symbol="CL=F",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=qty,
        time_in_force="GTC",
    )


@pytest.mark.asyncio
async def test_units_converted_to_whole_contracts() -> None:
    adapter = _adapter()
    contract = _fut_contract()
    order = _fut_order(Decimal("2000"))  # 2000 units / 1000 mult = 2 contracts
    await adapter._build_ib_order_for_contract(order, contract)
    assert order.quantity == Decimal("2")


@pytest.mark.asyncio
async def test_units_below_one_contract_raise() -> None:
    adapter = _adapter()
    contract = _fut_contract()
    order = _fut_order(Decimal("500"))  # 0.5 contract → not tradeable
    with pytest.raises(ValueError):
        await adapter._build_ib_order_for_contract(order, contract)


def test_contract_symbol_key_maps_back_to_continuous() -> None:
    adapter = _adapter()
    assert adapter._contract_symbol_key(_fut_contract("CL")) == "CL=F"
    assert adapter._contract_symbol_key(_fut_contract("ES")) == "ES=F"


def test_contract_multiplier_prefers_contract_then_spec() -> None:
    adapter = _adapter()
    # IB-qualified contract carries the multiplier → authoritative.
    assert adapter._contract_multiplier(_fut_contract("CL", mult="1000")) == Decimal("1000")
    # No multiplier on the contract → fall back to the static spec by root.
    bare = Future("ES", "202609", "CME", currency="USD")
    bare.multiplier = ""
    assert adapter._contract_multiplier(bare) == Decimal("50")


def test_contract_multiplier_one_for_non_futures() -> None:
    from ib_insync import Stock

    adapter = _adapter()
    assert adapter._contract_multiplier(Stock("AAPL", "SMART", "USD")) == Decimal("1")


def test_near_expiry_rollover_detection() -> None:
    adapter = _adapter()
    soon = (datetime.now(timezone.utc) + timedelta(days=2)).strftime("%Y%m%d")
    far = (datetime.now(timezone.utc) + timedelta(days=60)).strftime("%Y%m%d")
    assert adapter._futures_near_expiry(soon) is True
    assert adapter._futures_near_expiry(far) is False
    assert adapter._futures_near_expiry("") is False
