#!/usr/bin/env python3
"""
Minimal IBKR connectivity probe (no Telegram, no orders).

Steps:
1) connect
2) managedAccounts + client/server versions
3) fetch a small account summary (minimal tags; cancels request)

Usage:
  python test_ibkr_connectivity.py --paper
  python test_ibkr_connectivity.py --paper --account DUP1234567
"""

from __future__ import annotations

import argparse
import asyncio
import os

from dotenv import load_dotenv
from ib_insync import IB, util
from loguru import logger

util.patchAsyncio()


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="IBKR minimal connectivity probe")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--paper", action="store_true", help="Use paper port (default 7497)")
    mode.add_argument("--live", action="store_true", help="Use live port (default 7496)")
    p.add_argument("--timeout", type=float, default=None, help="Connect timeout (seconds)")
    p.add_argument("--account", default=None, help="Account id (e.g. DUP694288)")
    return p.parse_args()


async def main() -> None:
    load_dotenv()
    args = parse_args()

    paper_mode = True if args.paper else False if args.live else True
    host = os.getenv("IBKR_HOST", "127.0.0.1")
    port = int(os.getenv("IBKR_PAPER_PORT", "7497")) if paper_mode else int(
        os.getenv("IBKR_LIVE_PORT", os.getenv("IBKR_PORT", "7496"))
    )
    client_id = int(os.getenv("IBKR_CLIENT_ID", "7"))

    timeout = args.timeout
    if timeout is None:
        try:
            timeout = float(os.getenv("IBKR_CONNECT_TIMEOUT", "45"))
        except ValueError:
            timeout = 45.0

    ib = IB()
    logger.info("connectAsync | host={} port={} clientId={} timeout={}s", host, port, client_id, timeout)
    await ib.connectAsync(host=host, port=port, clientId=client_id, timeout=timeout, readonly=False)

    logger.info(
        "connected | serverVersion={} | isReady={} | managedAccounts={}",
        ib.client.serverVersion(),
        ib.client.isReady(),
        ib.managedAccounts(),
    )

    acct = (args.account or os.getenv("IBKR_ACCOUNT_ID", "") or "").strip()
    if not acct:
        accts = ib.managedAccounts()
        acct = accts[0] if accts else ""
    if not acct:
        logger.error("No account available (managedAccounts empty).")
        ib.disconnect()
        return

    tags = (os.getenv("IBKR_ACCOUNT_SUMMARY_TAGS", "") or "").strip() or "NetLiquidation,TotalCashValue,SettledCash,AvailableFunds"
    req_id = ib.client.getReqId()
    fut = ib.wrapper.startReq(req_id)
    ib.wrapper.acctSummary.clear()
    logger.info("reqAccountSummary | reqId={} account={} tags={}", req_id, acct, tags)
    ib.client.reqAccountSummary(req_id, "All", tags)

    # wait briefly for first rows
    deadline = asyncio.get_running_loop().time() + 12.0
    while asyncio.get_running_loop().time() < deadline and not ib.wrapper.acctSummary:
        await asyncio.sleep(0.05)

    rows = list(ib.wrapper.acctSummary.values())
    try:
        ib.client.cancelAccountSummary(req_id)
    except Exception as exc:  # noqa: BLE001
        logger.debug("cancelAccountSummary | {!r}", exc)

    try:
        await asyncio.wait_for(asyncio.shield(fut), timeout=3.0)
    except Exception:
        pass

    filt = [r for r in rows if r.account == acct] if acct else rows
    logger.info("accountSummary rows: total={} for_acct={}", len(rows), len(filt))
    for r in filt[:25]:
        logger.info("  {} {} {}={}", r.account, r.currency, r.tag, r.value)

    ib.disconnect()
    logger.info("done")


if __name__ == "__main__":
    asyncio.run(main())

