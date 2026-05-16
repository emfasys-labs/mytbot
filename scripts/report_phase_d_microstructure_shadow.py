"""Read-only report for Phase D microstructure execution shadow."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any

from sqlalchemy import select

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
except Exception:  # noqa: BLE001
    load_dotenv = None

from storage.db import dispose_engine, init_async_database  # noqa: E402
from storage.models import OrderLog  # noqa: E402


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Phase D microstructure shadow report")
    p.add_argument("--limit", type=int, default=100)
    return p.parse_args()


def summarize_microstructure_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    used = [r for r in rows if r.get("microstructure_shadow_used") is True]
    labels: dict[str, int] = {}
    for row in used:
        label = str(row.get("microstructure_shadow_label") or "unknown")
        labels[label] = labels.get(label, 0) + 1
    avg_risk = (
        sum(float(r.get("microstructure_shadow_risk") or 0.0) for r in used) / len(used)
        if used
        else 0.0
    )
    return {
        "rows": len(rows),
        "used": len(used),
        "labels": labels,
        "avg_risk": avg_risk,
    }


async def _run(args: argparse.Namespace) -> int:
    engine, factory = await init_async_database()
    if engine is None or factory is None:
        print("No database configured.")
        return 2
    try:
        async with factory() as session:
            q = await session.execute(select(OrderLog).order_by(OrderLog.timestamp.desc()).limit(max(1, int(args.limit))))
            orders = list(q.scalars().all())
        rows: list[dict[str, Any]] = []
        for order in orders:
            md = order.instrument_metadata if isinstance(order.instrument_metadata, dict) else {}
            if "microstructure_shadow_used" not in md:
                continue
            rows.append(
                {
                    "timestamp": order.timestamp.isoformat() if order.timestamp else None,
                    "symbol": order.symbol,
                    "broker": order.broker,
                    "side": order.side,
                    **md,
                }
            )
        summary = summarize_microstructure_rows(rows)
        print("Phase D microstructure shadow:")
        print(f"  scanned_orders={len(orders)}")
        print(f"  shadow_rows={summary['rows']}")
        print(f"  used={summary['used']}")
        print(f"  avg_risk={summary['avg_risk']:.4f}")
        print(f"  labels={summary['labels']}")
        if rows:
            print("\nRecent rows:")
            for row in rows[:10]:
                print(
                    "  "
                    f"{row.get('timestamp')} {row.get('broker')}:{row.get('symbol')} "
                    f"{row.get('side')} label={row.get('microstructure_shadow_label')} "
                    f"risk={row.get('microstructure_shadow_risk')} "
                    f"spread={row.get('microstructure_spread_bps')}"
                )
        return 0
    finally:
        await dispose_engine(engine)


def main() -> int:
    if load_dotenv is not None:
        load_dotenv(ROOT / ".env")
    return asyncio.run(_run(_parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
