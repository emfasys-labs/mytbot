"""
Manual integration test for IBKRAdapter against a local IB Gateway / TWS.

Streams BTC/USD, persists ticks to PostgreSQL (PriceHistory), places a small
crypto paper order (24/7). Loads IBKR and Postgres settings from .env.

Run from the repo root:

    python test_ibkr.py --paper

`--paper` connects to port **7497** by default (ignores `IBKR_PORT=7496` in `.env`).
Override with `IBKR_PAPER_PORT` if your Gateway uses a different paper socket.

Requires: IB Gateway on IBKR_HOST and the paper/live port, optional PostgreSQL for pipeline test.
"""

from __future__ import annotations

import asyncio
import argparse
import os
import time
import uuid
from decimal import Decimal

from dotenv import load_dotenv
from ib_insync import util
from loguru import logger

from brokers.base import Order, OrderSide, OrderStatus, OrderType, Tick
from brokers.ibkr.adapter import IBKRAdapter, _KNOWN_PAXOS_CRYPTO
from storage.db import dispose_engine, init_async_database, persist_price_tick

util.patchAsyncio()

# IBKR messages that usually mean US/equity session or NBBO data issues (not crypto).
_MARKET_SESSION_OR_DATA_PHRASES: tuple[str, ...] = (
    "no market data",
    "major exchange",
    "market is closed",
    "markets are closed",
    "exchange is closed",
    "outside of trading",
    "outside rth",
    "market closed",
    "trading hours",
    "non tradable",
    "session close",
    "regular trading",
)

STREAM_SYMBOL = "BTC"
ORDER_SYMBOL = "BTC"
ORDER_QTY = Decimal("0.001")
# Canonical label for stored prices (matches prior BTC/USD stream naming).
PRICE_DB_SYMBOL = "BTC/USD"
STREAM_SECONDS = 10.0


def _is_crypto_symbol(sym: str) -> bool:
    u = sym.strip().upper()
    if u in _KNOWN_PAXOS_CRYPTO:
        return True
    if "/" in u:
        return u.split("/", 1)[0].strip() in _KNOWN_PAXOS_CRYPTO
    return False


def _diagnostics_suggest_market_hours(text: str) -> bool:
    low = text.lower()
    return any(p in low for p in _MARKET_SESSION_OR_DATA_PHRASES)


def _log_order_outcome(
    adapter: IBKRAdapter,
    result,
    requested_symbol: str,
    *,
    time_in_force: str | None = None,
) -> None:
    diag = (
        adapter.order_cancel_diagnostics(result.broker_order_id)
        if result.broker_order_id
        else ""
    )
    crypto = _is_crypto_symbol(requested_symbol)
    st = result.status

    if st == OrderStatus.FILLED:
        logger.info(
            "test_ibkr | order | FILLED | broker_id={} | filled={}/{} | avg={}",
            result.broker_order_id,
            result.filled_quantity,
            result.quantity,
            result.avg_fill_price,
        )
        return

    if st in (OrderStatus.CANCELLED, OrderStatus.REJECTED):
        if diag:
            logger.info("test_ibkr | order | IB diagnostics | {}", diag)
        if not crypto and _diagnostics_suggest_market_hours(diag):
            logger.warning(
                "test_ibkr | order | Cancelled/rejected — likely **equities market hours / "
                "NBBO data** (e.g. weekend or no US stock market data subscription). "
                "IB text: {}",
                diag or "(no diagnostic text)",
            )
        elif crypto and _diagnostics_suggest_market_hours(diag):
            logger.warning(
                "test_ibkr | order | Crypto order failed but message looks like a **session / "
                "market-data** template from IB; check subscriptions and min order size. "
                "IB text: {}",
                diag or "(no diagnostic text)",
            )
        elif crypto and (
            "cash quantity" in diag.lower() or "10289" in diag
        ):
            logger.warning(
                "test_ibkr | order | PAXOS crypto needs **cashQty** (USD notional). "
                "The adapter should set this from base size × last price; if you still see "
                "this, check IB minimum notional and market data. IB text: {}",
                diag or "(no diagnostic text)",
            )
        elif crypto and (
            "not enough withdrawable cash" in diag.lower()
            or "available, [0.00 usd]" in diag.lower()
        ):
            logger.error(
                "test_ibkr | order | Rejected — insufficient **withdrawable USD** for crypto "
                "order settlement. Fund/convert USD in this account segment before retrying. "
                "IB text: {}",
                diag or "(no diagnostic text)",
            )
        elif (
            crypto
            and st == OrderStatus.CANCELLED
            and (time_in_force or "").strip().upper() == "IOC"
            and not result.filled_quantity
            and "error" not in diag.lower()
            and "reject" not in diag.lower()
        ):
            logger.warning(
                "test_ibkr | order | IOC crypto ended **Cancelled** with 0 fill — often normal "
                "on **paper** (nothing executed in the IOC window). Not the same as Error 201. "
                "IB log: {}",
                diag or "(no diagnostic text)",
            )
        else:
            logger.error(
                "test_ibkr | order | Cancelled/rejected — **treat as a real trading/config "
                "issue** (margin, size, permissions, connectivity). status={} | IB text: {}",
                st.value,
                diag or "(no diagnostic text)",
            )
        return

    if st == OrderStatus.PARTIALLY_FILLED:
        logger.warning(
            "test_ibkr | order | PARTIALLY_FILLED | broker_id={} | filled={}/{}",
            result.broker_order_id,
            result.filled_quantity,
            result.quantity,
        )
        return

    logger.info(
        "test_ibkr | order | status={} | broker_id={} | filled={}/{}",
        st.value,
        result.broker_order_id,
        result.filled_quantity,
        result.quantity,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="IBKR integration smoke test")
    parser.add_argument(
        "--live",
        action="store_true",
        help="Force live session (paper_mode=False, default port 7496 unless IBKR_PORT set).",
    )
    parser.add_argument(
        "--paper",
        action="store_true",
        help="Force paper session (paper_mode=True, default port 7497 unless IBKR_PORT set).",
    )
    args = parser.parse_args()
    if args.live and args.paper:
        raise SystemExit("Use only one of --live or --paper")
    return args


async def main(args: argparse.Namespace) -> None:
    load_dotenv()

    host = os.getenv("IBKR_HOST", "127.0.0.1")
    app_env = (os.getenv("APP_ENV", "paper") or "paper").strip().lower()
    force_live = args.live or app_env == "live"
    force_paper = args.paper or app_env == "paper"
    if args.live:
        paper_mode = False
    elif args.paper:
        paper_mode = True
    else:
        paper_mode = not force_live and force_paper
    default_port = "7497" if paper_mode else "7496"
    # IBKR_PORT in .env is often 7496 for live; explicit --paper must not pick that up.
    if args.paper:
        port = int(os.getenv("IBKR_PAPER_PORT", "7497"))
    elif args.live:
        port = int(os.getenv("IBKR_LIVE_PORT", os.getenv("IBKR_PORT", "7496")))
    else:
        port = int(os.getenv("IBKR_PORT", default_port))
    client_id = int(os.getenv("IBKR_CLIENT_ID", "1"))
    account_id = os.getenv("IBKR_ACCOUNT_ID", "").strip()

    adapter = IBKRAdapter(
        host=host,
        port=port,
        client_id=client_id,
        account_id=account_id,
        paper_mode=paper_mode,
    )

    engine, session_factory = await init_async_database()

    try:
        sync_timeout_s = float(os.getenv("IBKR_CONNECT_TIMEOUT", "15"))
    except ValueError:
        sync_timeout_s = 15.0
    logger.info(
        "test_ibkr | connecting to IBKR | host={} port={} | "
        "ib_insync uses {}s per sync step — brief pause is normal",
        host,
        port,
        sync_timeout_s,
    )
    try:
        connected = await adapter.connect()
    except Exception as exc:  # noqa: BLE001 — should not escape adapter.connect, but be safe
        logger.error(
            "test_ibkr | IBKR connect raised | {} | host={} port={} | "
            "start IB Gateway/TWS, enable API (Configure → Settings → API → "
            "Enable ActiveX and Socket Clients), confirm port matches IBKR_PORT "
            "(paper 7497 / live 7496).",
            exc,
            host,
            port,
        )
        await dispose_engine(engine)
        return
    if not connected:
        logger.error(
            "test_ibkr | connect failed | host={} port={} | "
            "is IB Gateway/TWS running with socket API on this port?",
            host,
            port,
        )
        await dispose_engine(engine)
        return

    ticks_written = 0
    try:
        balances = await adapter.get_balance()
        print("Account balances:")
        for b in balances:
            print(
                f"  {b.currency}: total={b.total} available={b.available} "
                f"reserved={b.reserved}"
            )

        logger.info(
            "test_ibkr | stream | symbol={} | duration_s={} | db={}",
            STREAM_SYMBOL,
            STREAM_SECONDS,
            "yes" if session_factory else "no",
        )
        print(f"\nStreaming {STREAM_SYMBOL} for {int(STREAM_SECONDS)} seconds...")
        deadline = time.monotonic() + STREAM_SECONDS
        async for tick in adapter.stream_prices([STREAM_SYMBOL]):
            print(
                f"  {tick.timestamp} {tick.symbol} last={tick.price} "
                f"bid={tick.bid} ask={tick.ask} vol={tick.volume}"
            )
            if session_factory:
                try:
                    await persist_price_tick(
                        session_factory,
                        tick,
                        db_symbol=PRICE_DB_SYMBOL,
                        broker="ibkr",
                    )
                    ticks_written += 1
                except Exception as exc:  # noqa: BLE001
                    logger.exception("test_ibkr | postgres | insert failed | error={}", exc)
            if time.monotonic() >= deadline:
                break

        if session_factory:
            logger.info(
                "test_ibkr | postgres | inserted {} tick row(s) into price_history "
                "(symbol={!r}, timeframe='tick', broker='ibkr')",
                ticks_written,
                PRICE_DB_SYMBOL,
            )
        else:
            logger.info("test_ibkr | postgres | no rows written (database not configured)")

        cid = str(uuid.uuid4())
        order = Order(
            symbol=ORDER_SYMBOL,
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            quantity=ORDER_QTY,
            client_order_id=cid,
            time_in_force="IOC",
        )
        mode_label = "live" if not adapter.paper_mode else "paper"
        print(
            f"\nPlacing {mode_label} MARKET BUY {ORDER_QTY} {ORDER_SYMBOL} "
            f"(IOC — IBKR PAXOS crypto MKT requires IOC)..."
        )
        result = await adapter.place_order(order)
        await asyncio.sleep(2)
        if result.broker_order_id:
            try:
                result = await adapter.get_order(result.broker_order_id)
            except (ValueError, ConnectionError):
                pass
        print(
            f"Order result: id={result.broker_order_id} status={result.status.value} "
            f"filled={result.filled_quantity}/{result.quantity} "
            f"avg={result.avg_fill_price} fee={result.fee}"
        )
        _log_order_outcome(adapter, result, ORDER_SYMBOL, time_in_force=order.time_in_force)
    finally:
        await adapter.disconnect()
        await dispose_engine(engine)
        print("\nDisconnected.")


if __name__ == "__main__":
    asyncio.run(main(parse_args()))
