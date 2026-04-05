"""
main.py
========
M1 entrypoint: concurrent **IBKR** and **Kraken** price streams, terminal logging,
optional **PostgreSQL** persistence (`price_history` tick rows), optional **IBKR**
paper crypto order + `orders` audit row.

Prerequisites:
  - Docker: `docker compose up -d` (TimescaleDB + Redis)
  - IB Gateway / TWS on ``IBKR_HOST:IBKR_PORT`` (paper 7497) for IBKR stream
  - ``KRAKEN_API_KEY`` / ``KRAKEN_API_SECRET`` for Kraken stream

Environment (optional):
  - ``M1_ENABLE_IBKR`` / ``M1_ENABLE_KRAKEN`` — default true
  - ``M1_IBKR_SYMBOLS`` — comma-separated, default ``BTC``
  - ``M1_KRAKEN_SYMBOLS`` — comma-separated, default ``BTC/USD``
  - ``M1_IBKR_PLACE_ORDER`` — set ``1`` to place one tiny paper **crypto** MKT (IOC) after delay
  - ``M1_IBKR_ORDER_DELAY_SEC`` — default ``20``
  - ``M1_IBKR_ORDER_SYMBOL`` / ``M1_IBKR_ORDER_QTY`` — default ``BTC`` / ``0.001``

Usage (from repo root, with venv — see README):

    .venv/Scripts/python.exe main.py   # Windows
    .venv/bin/python main.py           # macOS / Linux

**Lifetime:** runs **until you stop it** (Ctrl+C). Stream tasks are long-running by design;
the optional IBKR order task exits on its own when disabled.
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from decimal import Decimal

from dotenv import load_dotenv
from ib_insync import util
from loguru import logger

util.patchAsyncio()

from brokers.base import Order, OrderSide, OrderType, Tick
from brokers.ibkr.adapter import IBKRAdapter, _KNOWN_PAXOS_CRYPTO
from brokers.kraken.adapter import KrakenAdapter
from storage.db import dispose_engine, init_async_database, persist_order_log, persist_price_tick


def _env_bool(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _parse_symbol_list(value: str | None, default_csv: str) -> list[str]:
    raw = (value or default_csv).strip()
    parts = [x.strip() for x in raw.split(",") if x.strip()]
    return parts or [default_csv.split(",")[0].strip()]


def _db_symbol_ibkr(tick_symbol: str) -> str:
    u = tick_symbol.strip().upper()
    if "/" in u:
        return u[:20]
    if u in _KNOWN_PAXOS_CRYPTO:
        return f"{u}/USD"[:20]
    return u[:20]


def _configure_logging() -> None:
    level = os.getenv("LOG_LEVEL", "INFO").upper()
    logger.remove()
    logger.add(sys.stderr, level=level)


async def run_m1() -> None:
    load_dotenv()
    _configure_logging()

    app_env = os.getenv("APP_ENV", "paper")
    paper_mode = app_env.strip().lower() != "live"
    if not paper_mode:
        logger.warning("APP_ENV=live — brokers use live mode where applicable; be careful.")

    engine, session_factory = await init_async_database()

    ibkr_on = _env_bool("M1_ENABLE_IBKR", True)
    kraken_on = _env_bool("M1_ENABLE_KRAKEN", True)
    ibkr_symbols = _parse_symbol_list(os.getenv("M1_IBKR_SYMBOLS"), "BTC")
    kraken_symbols = _parse_symbol_list(os.getenv("M1_KRAKEN_SYMBOLS"), "BTC/USD")
    place_ibkr_order = _env_bool("M1_IBKR_PLACE_ORDER", False)
    order_delay = float(os.getenv("M1_IBKR_ORDER_DELAY_SEC", "20"))
    order_qty = Decimal(os.getenv("M1_IBKR_ORDER_QTY", "0.001"))
    order_symbol = os.getenv("M1_IBKR_ORDER_SYMBOL", "BTC").strip() or "BTC"

    ibkr = IBKRAdapter(
        host=os.getenv("IBKR_HOST", "127.0.0.1"),
        port=int(os.getenv("IBKR_PORT", "7497")),
        client_id=int(os.getenv("IBKR_CLIENT_ID", "1")),
        account_id=os.getenv("IBKR_ACCOUNT_ID", "").strip(),
        paper_mode=paper_mode,
    )
    kraken = KrakenAdapter(
        api_key=os.getenv("KRAKEN_API_KEY", "").strip(),
        api_secret=os.getenv("KRAKEN_API_SECRET", "").strip(),
        paper_mode=paper_mode,
    )

    ibkr_ticks = 0
    kraken_ticks = 0

    async def ibkr_stream() -> None:
        nonlocal ibkr_ticks
        if not ibkr_on:
            logger.info("M1 | IBKR | disabled (M1_ENABLE_IBKR=0)")
            return
        connected = await ibkr.connect()
        if not connected:
            logger.warning(
                "M1 | IBKR | not connected — start IB Gateway/TWS, API enabled, port {}",
                os.getenv("IBKR_PORT", "7497"),
            )
            return
        logger.info("M1 | IBKR | streaming {}", ibkr_symbols)
        try:
            async for tick in ibkr.stream_prices(ibkr_symbols):
                db_sym = _db_symbol_ibkr(tick.symbol)
                _print_tick("ibkr", tick)
                if session_factory:
                    try:
                        await persist_price_tick(
                            session_factory,
                            tick,
                            db_symbol=db_sym,
                            broker="ibkr",
                        )
                        ibkr_ticks += 1
                    except Exception as exc:  # noqa: BLE001
                        logger.exception("M1 | postgres | ibkr tick insert | {}", exc)
        except asyncio.CancelledError:
            raise
        finally:
            logger.info("M1 | IBKR | stream stopped | ticks_persisted={}", ibkr_ticks)

    async def kraken_stream() -> None:
        nonlocal kraken_ticks
        if not kraken_on:
            logger.info("M1 | Kraken | disabled (M1_ENABLE_KRAKEN=0)")
            return
        if not kraken.api_key or not kraken.api_secret:
            logger.warning("M1 | Kraken | missing KRAKEN_API_KEY / KRAKEN_API_SECRET — stream skipped")
            return
        if not await kraken.connect():
            logger.warning("M1 | Kraken | connect failed — stream skipped")
            return
        logger.info("M1 | Kraken | streaming {}", kraken_symbols)
        try:
            async for tick in kraken.stream_prices(kraken_symbols):
                db_sym = tick.symbol.strip()[:20]
                _print_tick("kraken", tick)
                if session_factory:
                    try:
                        await persist_price_tick(
                            session_factory,
                            tick,
                            db_symbol=db_sym,
                            broker="kraken",
                        )
                        kraken_ticks += 1
                    except Exception as exc:  # noqa: BLE001
                        logger.exception("M1 | postgres | kraken tick insert | {}", exc)
        except asyncio.CancelledError:
            raise
        finally:
            logger.info("M1 | Kraken | stream stopped | ticks_persisted={}", kraken_ticks)

    async def ibkr_optional_order() -> None:
        if not place_ibkr_order or not ibkr_on:
            return
        await asyncio.sleep(max(order_delay, 0.0))
        if not await ibkr.is_connected():
            logger.warning("M1 | IBKR | paper order skipped (not connected)")
            return
        cid = str(uuid.uuid4())
        order = Order(
            symbol=order_symbol,
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            quantity=order_qty,
            client_order_id=cid,
            time_in_force="IOC",
        )
        logger.info(
            "M1 | IBKR | paper order | {} {} MKT IOC | client_id={}",
            order_qty,
            order_symbol,
            cid[:8],
        )
        result = await ibkr.place_order(order)
        await asyncio.sleep(1.5)
        if result.broker_order_id:
            try:
                result = await ibkr.get_order(result.broker_order_id)
            except Exception:  # noqa: BLE001
                pass
        logger.info(
            "M1 | IBKR | order result | broker_id={} status={} filled={}/{} avg={}",
            result.broker_order_id,
            result.status.value,
            result.filled_quantity,
            result.quantity,
            result.avg_fill_price,
        )
        if session_factory:
            try:
                await persist_order_log(
                    session_factory,
                    order=order,
                    result=result,
                    signal_id="m1-bootstrap",
                    paper_mode=paper_mode,
                    broker="ibkr",
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception("M1 | postgres | order log insert | {}", exc)

    tasks = [
        asyncio.create_task(ibkr_stream(), name="m1-ibkr-stream"),
        asyncio.create_task(kraken_stream(), name="m1-kraken-stream"),
        asyncio.create_task(ibkr_optional_order(), name="m1-ibkr-order"),
    ]

    logger.info(
        "M1 | start | APP_ENV={} postgres={} ibkr={} kraken={} ibkr_order={}",
        app_env,
        "yes" if session_factory else "no",
        ibkr_on,
        kraken_on,
        place_ibkr_order,
    )

    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
    finally:
        await ibkr.disconnect()
        await kraken.disconnect()
        await dispose_engine(engine)
        logger.info("M1 | shutdown | ibkr_ticks={} kraken_ticks={}", ibkr_ticks, kraken_ticks)


def _print_tick(source: str, tick: Tick) -> None:
    bid_s = str(tick.bid) if tick.bid is not None else "—"
    ask_s = str(tick.ask) if tick.ask is not None else "—"
    print(
        f"[{source}] {tick.timestamp} {tick.symbol} "
        f"last={tick.price} bid={bid_s} ask={ask_s} vol={tick.volume}",
        flush=True,
    )


async def main() -> None:
    await run_m1()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("M1 | interrupted — exiting")
