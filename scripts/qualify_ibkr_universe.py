from __future__ import annotations

import argparse
import asyncio
import os
from collections import Counter

from brokers.ibkr.adapter import IBKRAdapter
from brokers.ibkr.universe import load_ibkr_universe


def _paper_mode() -> bool:
    return str(os.getenv("APP_ENV", "paper")).strip().lower() != "live"


async def _main() -> int:
    parser = argparse.ArgumentParser(description="Qualify the curated IBKR universe and update the local cache.")
    parser.add_argument("--limit", type=int, default=0, help="Maximum number of instruments to qualify.")
    parser.add_argument("--symbol", action="append", default=[], help="Only qualify this broker symbol; repeatable.")
    args = parser.parse_args()

    wanted = {str(s).strip().upper() for s in args.symbol if str(s).strip()}
    entries = [e for e in load_ibkr_universe() if not wanted or e.broker_symbol.upper() in wanted]
    if args.limit and args.limit > 0:
        entries = entries[: args.limit]

    adapter = IBKRAdapter(
        host=os.getenv("IBKR_HOST", "127.0.0.1"),
        port=int(os.getenv("IBKR_PORT", "7497")),
        client_id=int(os.getenv("IBKR_CLIENT_ID", "1")),
        account_id=os.getenv("IBKR_ACCOUNT_ID", ""),
        paper_mode=_paper_mode(),
    )
    ok = await adapter.connect()
    if not ok:
        print("IBKR connect failed; no contracts qualified.")
        return 2

    counts: Counter[str] = Counter()
    try:
        for entry in entries:
            rec = await adapter.qualify_symbol(entry.broker_symbol, entry.asset_class, force=True)
            counts[rec.status] += 1
            detail = f" conId={rec.con_id}" if rec.con_id else f" error={rec.error}"
            print(f"{rec.status:9s} {entry.broker_symbol:12s} {entry.asset_class:7s}{detail}")
    finally:
        await adapter.disconnect()

    print(f"qualified={counts['qualified']} failed={counts['failed']} total={sum(counts.values())}")
    return 0 if counts["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))

