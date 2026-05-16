"""Readiness report for Phase C shadow-policy evaluation."""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
except Exception:  # noqa: BLE001
    load_dotenv = None

from control.command_bus import CommandBus  # noqa: E402
from scripts.evaluate_phase_c_transition_history import _parse_ts  # noqa: E402
from storage.db import dispose_engine, init_async_database  # noqa: E402
from system.dashboard_publish import REGIME_TRANSITION_SHADOW_HISTORY_KEY  # noqa: E402


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Phase C shadow evaluation readiness")
    p.add_argument("--horizon-hours", type=int, default=4)
    p.add_argument("--min-mature-rows", type=int, default=24)
    return p.parse_args()


def summarize_readiness(
    rows: list[dict[str, Any]],
    *,
    now: datetime,
    horizon: timedelta,
    min_mature_rows: int,
) -> dict[str, Any]:
    timestamps = [_parse_ts(r.get("timestamp")) for r in rows]
    timestamps = [t for t in timestamps if t is not None]
    mature = [t for t in timestamps if t + horizon <= now]
    next_ready_at = None
    pending = sorted(t + horizon for t in timestamps if t + horizon > now)
    if pending:
        next_ready_at = pending[0]
    return {
        "history_rows": len(rows),
        "timestamped_rows": len(timestamps),
        "mature_rows": len(mature),
        "min_mature_rows": int(min_mature_rows),
        "ready": len(mature) >= int(min_mature_rows),
        "oldest_row": min(timestamps).isoformat() if timestamps else None,
        "newest_row": max(timestamps).isoformat() if timestamps else None,
        "next_ready_at": next_ready_at.isoformat() if next_ready_at else None,
    }


async def _run(args: argparse.Namespace) -> int:
    engine, factory = await init_async_database()
    if engine is None or factory is None:
        print("No database configured.")
        return 2
    try:
        bus = CommandBus(factory)
        raw = await bus.get_state(REGIME_TRANSITION_SHADOW_HISTORY_KEY, [])
        rows = [r for r in raw if isinstance(r, dict)] if isinstance(raw, list) else []
        summary = summarize_readiness(
            rows,
            now=datetime.now(timezone.utc),
            horizon=timedelta(hours=max(1, int(args.horizon_hours))),
            min_mature_rows=max(1, int(args.min_mature_rows)),
        )
        print("Phase C evaluation readiness:")
        for key, value in summary.items():
            print(f"  {key}={value}")
        return 0
    finally:
        await dispose_engine(engine)


def main() -> int:
    if load_dotenv is not None:
        load_dotenv(ROOT / ".env")
    return asyncio.run(_run(_parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
