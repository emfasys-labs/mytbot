#!/usr/bin/env python3
"""
Summarise recent D015-relevant activity from the database (paper or live).

Usage (from repo root, with POSTGRES_* in .env):
  .venv\\Scripts\\python.exe scripts/d015_paper_report.py
"""

from __future__ import annotations

import asyncio
from collections import Counter
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from storage.db import init_async_database
from storage.models import SignalLog


async def _run() -> None:
    engine, session_factory = await init_async_database()
    if session_factory is None:
        print("Database unavailable.")
        return
    try:
        since = datetime.now(timezone.utc) - timedelta(days=7)
        async with session_factory() as session:
            q = await session.execute(
                select(SignalLog)
                .where(SignalLog.timestamp >= since)
                .order_by(SignalLog.timestamp.desc())
                .limit(5000)
            )
            rows = list(q.scalars().all())
        strat = Counter()
        sym = Counter()
        d015_exec = 0
        for r in rows:
            meta = r.metadata_ if isinstance(r.metadata_, dict) else {}
            if not isinstance(meta, dict):
                meta = {}
            st = str(getattr(r, "strategy", "") or meta.get("strategy") or "")
            strat[st] += 1
            sym[str(r.symbol or "")] += 1
            if meta.get("d015_executor") or st == "d015_allocator":
                d015_exec += 1
        print("D015 paper report (last 7 days)")
        print(f"  signal_log_rows: {len(rows)}")
        print(f"  instructions_via_d015_executor_meta: {d015_exec}")
        print("  top strategies:", dict(strat.most_common(8)))
        print("  top symbols:", dict(sym.most_common(12)))
    finally:
        if engine is not None:
            from storage.db import dispose_engine

            await dispose_engine(engine)


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
