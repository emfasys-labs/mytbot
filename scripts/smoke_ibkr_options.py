#!/usr/bin/env python3
"""
Smoke test: IBKR paper option chain + qualify + optional single long call order.

Requires: TWS or IB Gateway on IBKR_HOST/IBKR_PORT (paper default 7497).

  python scripts/smoke_ibkr_options.py

Optional paper order (limit far above market — adjust or use bid/ask):

  set MYTBOT_IBKR_OPTIONS_PLACE_ORDER=1
"""

from __future__ import annotations

import asyncio
import os
import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from brokers.base import Order, OrderSide, OrderType
from brokers.ibkr.adapter import IBKRAdapter
from core.instruments import OptionContractSpec, OptionRight


async def main() -> int:
    host = os.getenv("IBKR_HOST", "127.0.0.1")
    try:
        port = int(os.getenv("IBKR_PORT", "7497"))
    except ValueError:
        port = 7497
    paper = (os.getenv("APP_ENV", "paper") or "paper").strip().lower() != "live"
    adapter = IBKRAdapter(host=host, port=port, paper_mode=paper)
    ok = await adapter.connect()
    if not ok:
        print("smoke_ibkr_options | connect failed")
        return 1

    underlying = (os.getenv("SMOKE_OPTIONS_UNDERLYING", "SPY") or "SPY").strip().upper()
    chain = await adapter.get_option_chain(underlying)
    if not chain:
        print("smoke_ibkr_options | empty option chain (market data subscription or symbol?)")
        await adapter.disconnect()
        return 1

    print("smoke_ibkr_options | chain slices:", len(chain))
    slice0 = chain[0]
    expirations = slice0.get("expirations") or []
    strikes = slice0.get("strikes") or []
    if not expirations or not strikes:
        print("smoke_ibkr_options | no expirations/strikes in first slice")
        await adapter.disconnect()
        return 1

    expiry = sorted(expirations)[0]
    strikes_d = sorted(strikes)
    mid = strikes_d[len(strikes_d) // 2]
    mult = int(slice0.get("multiplier") or 100)

    spec = OptionContractSpec(
        underlying_symbol=underlying,
        expiry=expiry,
        strike=mid,
        right=OptionRight.CALL,
        multiplier=mult,
        exchange=str(slice0.get("exchange") or "SMART"),
    )
    print("smoke_ibkr_options | spec:", spec.position_key())

    q = await adapter.qualify_option_contract(spec)
    print("smoke_ibkr_options | qualified conId=", getattr(q, "conId", None), "local=", getattr(q, "localSymbol", None))

    md = await adapter.get_option_market_data(spec)
    print("smoke_ibkr_options | mkt", md)

    place = (os.getenv("MYTBOT_IBKR_OPTIONS_PLACE_ORDER", "") or "").strip().lower() in ("1", "true", "yes", "on")
    if place:
        lim_raw = os.getenv("SMOKE_OPTIONS_LIMIT_PRICE", "").strip()
        if lim_raw:
            lim = Decimal(lim_raw)
        else:
            ask = md.get("ask")
            lim = ask if ask is not None else Decimal("999")
        order = Order(
            symbol=spec.position_key(),
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            quantity=Decimal("1"),
            limit_price=lim,
            client_order_id=f"smoke-opt-{expiry}-{mid}",
            instrument_metadata={
                "instrument_type": "option",
                "option_contract": spec.to_dict(),
            },
        )
        res = await adapter.place_order(order)
        print("smoke_ibkr_options | place_order", res)
    else:
        print("smoke_ibkr_options | skip place_order (set MYTBOT_IBKR_OPTIONS_PLACE_ORDER=1 to test)")

    await adapter.disconnect()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
