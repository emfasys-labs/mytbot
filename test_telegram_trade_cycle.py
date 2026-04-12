#!/usr/bin/env python3
"""
Telegram + broker smoke: balances → open → notify → hold → close → notify → balances.

Loads `.env` (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, broker credentials).

Default: IBKR paper. **PAXOS crypto** (e.g. ``BTC``, ``ETH``) frequently goes
**Inactive** on IBKR paper and may not produce fills — do not rely on it for a
full fill-cycle smoke test. For US paper accounts a liquid ETF such as ``SPY``
is often best; for UK/EU retail (KID/PRIIPs) many US ETFs are blocked, so use
FX (``EURUSD``) instead.

Symbol hints: IBKR crypto often ``BTC``; Kraken ``BTC/USD``; Binance/Bybit spot or linear ``BTCUSDT``.

Examples:
  python test_telegram_trade_cycle.py --paper
  python test_telegram_trade_cycle.py --paper --symbol EURUSD --qty 1000 --hold-sec 2
  python test_telegram_trade_cycle.py --broker kraken --symbol BTC/USD --qty 0.0001 --paper
"""

from __future__ import annotations

import argparse
import asyncio
import os
import uuid
from decimal import Decimal

import httpx
from dotenv import load_dotenv
from ib_insync import util
from loguru import logger

from brokers.base import Balance, Order, OrderResult, OrderSide, OrderStatus, OrderType
from brokers.registry import get_broker

util.patchAsyncio()

_CRYPTOISH = frozenset(
    "btc eth xrp doge ltc sol ada dot bch link uni atom xlm algo near matic avax".split()
)


def _is_likely_crypto_symbol(sym: str) -> bool:
    u = sym.strip().replace("/", " ")
    parts = u.split()
    return any(p.lower() in _CRYPTOISH for p in parts)


def _default_tif(symbol: str) -> str:
    return "IOC" if _is_likely_crypto_symbol(symbol) else "DAY"


# IBKR PAXOS spot bases (align with brokers.ibkr.adapter._KNOWN_PAXOS_CRYPTO)
_IBKR_PAXOS_BASES = frozenset(
    {
        "BTC",
        "ETH",
        "LTC",
        "BCH",
        "PAXG",
        "SOL",
        "ADA",
        "DOGE",
        "LINK",
        "MATIC",
        "DOT",
    }
)


def _ibkr_likely_paxos_spot(symbol: str) -> bool:
    u = symbol.strip().upper().replace("/", " ")
    base = u.split()[0]
    return base in _IBKR_PAXOS_BASES


async def send_telegram(text: str) -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        raise SystemExit(
            "Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID in environment (.env)."
        )
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": f"[mytbot] {text}"}
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.post(url, json=payload)
        r.raise_for_status()


def _fmt_balances(rows: list[Balance]) -> str:
    if not rows:
        return "(no balance rows returned)"
    lines = [
        f"{b.currency}: total={b.total} available={b.available} reserved={b.reserved}"
        for b in rows
    ]
    return "\n".join(lines)


def _fmt_order_result(prefix: str, r, symbol: str) -> str:
    return (
        f"{prefix}\n"
        f"symbol={symbol}\n"
        f"broker_id={r.broker_order_id!r}\n"
        f"status={r.status.value}\n"
        f"filled={r.filled_quantity}/{r.quantity}\n"
        f"avg={r.avg_fill_price}\n"
        f"fee={r.fee}"
    )


def _ibkr_connect_kwargs(paper_mode: bool) -> dict:
    host = os.getenv("IBKR_HOST", "127.0.0.1")
    default_port = "7497" if paper_mode else "7496"
    if paper_mode:
        port = int(os.getenv("IBKR_PAPER_PORT", "7497"))
    else:
        port = int(os.getenv("IBKR_LIVE_PORT", os.getenv("IBKR_PORT", default_port)))
    return {
        "host": host,
        "port": port,
        "client_id": int(os.getenv("IBKR_CLIENT_ID", "1")),
        "account_id": os.getenv("IBKR_ACCOUNT_ID", "").strip(),
    }


def _make_broker(name: str, paper_mode: bool):
    n = name.strip().lower()
    if n == "ibkr":
        return get_broker("ibkr", paper_mode=paper_mode, **_ibkr_connect_kwargs(paper_mode))
    if n == "kraken":
        return get_broker(
            "kraken",
            paper_mode=paper_mode,
            api_key=os.getenv("KRAKEN_API_KEY", "").strip(),
            api_secret=os.getenv("KRAKEN_API_SECRET", "").strip(),
        )
    if n == "binance":
        return get_broker(
            "binance",
            paper_mode=paper_mode,
            api_key=os.getenv("BINANCE_API_KEY", "").strip(),
            api_secret=os.getenv("BINANCE_API_SECRET", "").strip(),
            testnet=os.getenv("BINANCE_TESTNET", "0").strip().lower() in {"1", "true", "yes", "on"},
        )
    if n == "bybit":
        return get_broker(
            "bybit",
            paper_mode=paper_mode,
            api_key=os.getenv("BYBIT_API_KEY", "").strip(),
            api_secret=os.getenv("BYBIT_API_SECRET", "").strip(),
            testnet=os.getenv("BYBIT_TESTNET", "0").strip().lower() in {"1", "true", "yes", "on"},
            category=(os.getenv("BYBIT_CATEGORY", "linear") or "linear").strip().lower(),
        )
    raise SystemExit(f"Unsupported broker: {name}")


async def _refresh_order(adapter, broker_id: str, fallback):
    if not broker_id:
        return fallback
    try:
        return await adapter.get_order(broker_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("get_order refresh failed | {} | {}", broker_id, exc)
        return fallback


async def _wait_open_fill(
    adapter,
    broker_id: str,
    fallback,
    *,
    max_wait_sec: int,
    poll_interval_sec: float,
) -> OrderResult:
    """Poll get_order until filled, rejected/cancelled, or timeout."""
    res = fallback
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(1, max_wait_sec)
    while loop.time() < deadline:
        res = await _refresh_order(adapter, broker_id, res)
        fill = res.filled_quantity
        if fill is not None and fill > 0:
            return res
        if res.status in (OrderStatus.CANCELLED, OrderStatus.REJECTED):
            return res
        await asyncio.sleep(max(0.5, poll_interval_sec))
    return res


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Telegram notifications around a tiny open/hold/close cycle")
    p.add_argument("--broker", default="ibkr", help="ibkr | kraken | binance | bybit")
    p.add_argument("--symbol", default=None, help="Symbol (defaults depend on broker; env TEST_TG_SYMBOL overrides)")
    p.add_argument("--qty", default=None, help="Quantity (env TEST_TG_QTY overrides; else computed from symbol)")
    p.add_argument("--hold-sec", type=int, default=60, help="Seconds to wait between open and close")
    p.add_argument(
        "--fill-wait-sec",
        type=int,
        default=None,
        help="IBKR: max seconds to poll open-leg fill (default TEST_TG_FILL_WAIT_SEC or 90)",
    )
    p.add_argument(
        "--tif",
        default=None,
        help="Time in force (default IOC for crypto-like symbols, else DAY for IBKR)",
    )
    p.add_argument(
        "--ibkr-client-id",
        type=int,
        default=None,
        help="Override IBKR client id (useful if 'client id already in use')",
    )
    p.add_argument(
        "--ibkr-account-id",
        default=None,
        help="Override IBKR account id (e.g. DUP694288). If unset, adapter auto-resolves.",
    )
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--paper", action="store_true", help="Paper / sandbox (default if APP_ENV=paper)")
    mode.add_argument("--live", action="store_true", help="Live orders — real money when broker is live")
    return p.parse_args()


async def main() -> None:
    load_dotenv()
    args = parse_args()
    app_env = (os.getenv("APP_ENV", "paper") or "paper").strip().lower()
    if args.live:
        paper_mode = False
    elif args.paper:
        paper_mode = True
    else:
        paper_mode = app_env != "live"

    broker_name = args.broker.strip().lower()

    env_symbol = (os.getenv("TEST_TG_SYMBOL", "") or "").strip()
    if args.symbol:
        symbol = str(args.symbol).strip()
    elif env_symbol:
        symbol = env_symbol
    else:
        if broker_name == "ibkr":
            symbol = "EURUSD"
        elif broker_name == "kraken":
            symbol = "BTC/USD"
        elif broker_name in ("binance", "bybit"):
            symbol = "BTCUSDT"
        else:
            symbol = "BTC"

    env_qty = (os.getenv("TEST_TG_QTY", "") or "").strip()
    if args.qty is not None:
        qty = Decimal(str(args.qty))
    elif env_qty:
        qty = Decimal(env_qty)
    else:
        u = symbol.strip().upper().replace("/", "")
        if _is_likely_crypto_symbol(symbol) or u.endswith("USDT") or u.endswith("USDC"):
            qty = Decimal("0.001")
        elif len(u) == 6 and u.isalpha():
            qty = Decimal("1000")
        else:
            qty = Decimal("1")

    tif = (args.tif or _default_tif(symbol)).strip()

    if qty <= 0:
        raise SystemExit("--qty must be positive")

    if broker_name == "ibkr":
        if args.ibkr_client_id is not None:
            os.environ["IBKR_CLIENT_ID"] = str(int(args.ibkr_client_id))
        if args.ibkr_account_id is not None:
            os.environ["IBKR_ACCOUNT_ID"] = str(args.ibkr_account_id).strip()
    adapter = _make_broker(broker_name, paper_mode)

    await send_telegram(
        f"Trade cycle START\n"
        f"broker={broker_name} paper_mode={paper_mode}\n"
        f"symbol={symbol} qty={qty} hold={args.hold_sec}s tif={tif}\n"
        f"Connecting…"
    )

    if not await adapter.connect():
        await send_telegram(f"ERROR: connect failed for {broker_name}")
        raise SystemExit(1)

    await send_telegram("Connected. Fetching balances…")
    logger.info("{} connected | fetching balances…", broker_name.upper())

    try:
        bal0 = await adapter.get_balance()
        bal0_msg = f"Balances (before):\n{_fmt_balances(bal0)}"
        if broker_name == "ibkr" and not bal0:
            bal0_msg += (
                "\n\n(Empty: summary may have timed out — set IBKR_ACCOUNT_SUMMARY_TIMEOUT, "
                "set IBKR_ACCOUNT_ID, raise IBKR_CONNECT_TIMEOUT (e.g. 120) for full sync, "
                "or restart Gateway.)"
            )
        await send_telegram(bal0_msg)

        ibkr_fill_wait = 90
        ibkr_poll_iv = 3.0
        if broker_name == "ibkr":
            ibkr_fill_wait = int(args.fill_wait_sec or os.getenv("TEST_TG_FILL_WAIT_SEC", "90"))
            ibkr_poll_iv = float(os.getenv("TEST_TG_FILL_POLL_INTERVAL", "3"))

        open_id = str(uuid.uuid4())
        open_order = Order(
            symbol=symbol,
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            quantity=qty,
            client_order_id=open_id,
            time_in_force=tif,
        )
        logger.info("OPEN | {} BUY {} tif={}", symbol, qty, tif)
        open_res = await adapter.place_order(open_order)
        if broker_name == "ibkr":
            open_res = await _wait_open_fill(
                adapter,
                open_res.broker_order_id,
                open_res,
                max_wait_sec=ibkr_fill_wait,
                poll_interval_sec=ibkr_poll_iv,
            )
        else:
            await asyncio.sleep(2.0)
            open_res = await _refresh_order(adapter, open_res.broker_order_id, open_res)

        await send_telegram(_fmt_order_result("OPEN leg", open_res, symbol))

        fill = open_res.filled_quantity
        if fill is None or fill <= 0:
            diag = ""
            if broker_name == "ibkr" and hasattr(adapter, "order_cancel_diagnostics"):
                raw = adapter.order_cancel_diagnostics(open_res.broker_order_id)
                if raw:
                    diag = raw[:3500]

            if (
                broker_name == "ibkr"
                and paper_mode
                and _ibkr_likely_paxos_spot(symbol)
                and open_res.status == OrderStatus.REJECTED
            ):
                msg = (
                    "OPEN leg → REJECTED/Inactive (typical on IBKR *paper* for PAXOS crypto: "
                    "paper simulator does not fill Paxos-routed crypto).\n"
                    "Telegram + adapter wiring is OK — for a full fill cycle on paper use equity, e.g.\n"
                    "  python test_telegram_trade_cycle.py --paper --symbol SPY --qty 1"
                )
                if diag:
                    msg += f"\n\nIBKR diagnostics:\n{diag}"
                await send_telegram(msg)
            else:
                await send_telegram(
                    "No fill on open — skipping close. "
                    "(IBKR: raise IBKR_CONNECT_TIMEOUT, IBKR_ORDER_REFRESH_TIMEOUT; "
                    "paper PAXOS crypto often rejects; try --symbol SPY --qty 1; "
                    "Binance/Bybit paper_mode does not submit live orders.)"
                    + (f"\n\nIBKR diagnostics:\n{diag}" if diag else "")
                )
            bal1 = await adapter.get_balance()
            await send_telegram(f"Balances (after open attempt):\n{_fmt_balances(bal1)}")
            return

        logger.info("HOLD | {}s", args.hold_sec)
        await asyncio.sleep(max(1, int(args.hold_sec)))

        close_id = str(uuid.uuid4())
        close_order = Order(
            symbol=symbol,
            side=OrderSide.SELL,
            order_type=OrderType.MARKET,
            quantity=fill,
            client_order_id=close_id,
            time_in_force=tif,
        )
        logger.info("CLOSE | {} SELL {} tif={}", symbol, fill, tif)
        close_res = await adapter.place_order(close_order)
        if broker_name == "ibkr":
            close_res = await _wait_open_fill(
                adapter,
                close_res.broker_order_id,
                close_res,
                max_wait_sec=ibkr_fill_wait,
                poll_interval_sec=ibkr_poll_iv,
            )
        else:
            await asyncio.sleep(2.0)
            close_res = await _refresh_order(adapter, close_res.broker_order_id, close_res)

        await send_telegram(_fmt_order_result("CLOSE leg", close_res, symbol))

        bal2 = await adapter.get_balance()
        await send_telegram(
            f"Trade cycle DONE\nBalances (after):\n{_fmt_balances(bal2)}"
        )
    finally:
        await adapter.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
