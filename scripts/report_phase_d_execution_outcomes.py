"""Read-only outcome join for Phase D microstructure shadow."""

from __future__ import annotations

import argparse
import asyncio
import sys
from decimal import Decimal
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
    p = argparse.ArgumentParser(description="Phase D microstructure shadow outcome report")
    p.add_argument("--limit", type=int, default=250)
    return p.parse_args()


def slippage_bps(*, side: str, reference_price: Any, fill_price: Any) -> float | None:
    try:
        ref = Decimal(str(reference_price))
        fill = Decimal(str(fill_price))
    except Exception:  # noqa: BLE001
        return None
    if ref <= 0 or fill <= 0:
        return None
    side_l = str(side or "").strip().lower()
    if side_l == "buy":
        return float((fill - ref) / ref * Decimal("10000"))
    if side_l == "sell":
        return float((ref - fill) / ref * Decimal("10000"))
    return None


def summarize_outcomes(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_label: dict[str, dict[str, Any]] = {}
    for row in rows:
        label = str(row.get("microstructure_shadow_label") or "unknown")
        bucket = by_label.setdefault(
            label,
            {"count": 0, "filled": 0, "slippage_samples": 0, "slippage_sum": 0.0, "fee_sum": 0.0},
        )
        bucket["count"] += 1
        if row.get("filled"):
            bucket["filled"] += 1
        slip = row.get("realized_slippage_bps")
        if slip is not None:
            bucket["slippage_samples"] += 1
            bucket["slippage_sum"] += float(slip)
        fee = row.get("fee")
        if fee is not None:
            bucket["fee_sum"] += float(fee)
    for bucket in by_label.values():
        bucket["fill_rate"] = bucket["filled"] / bucket["count"] if bucket["count"] else 0.0
        bucket["avg_slippage_bps"] = (
            bucket["slippage_sum"] / bucket["slippage_samples"] if bucket["slippage_samples"] else 0.0
        )
    return {"rows": len(rows), "by_label": by_label}


def order_to_outcome_row(order: Any) -> dict[str, Any] | None:
    md = order.instrument_metadata if isinstance(order.instrument_metadata, dict) else {}
    if "microstructure_shadow_used" not in md:
        return None
    filled_qty = Decimal(str(order.filled_quantity or 0))
    fill_px = order.avg_fill_price
    ref_px = order.limit_price
    slip = slippage_bps(side=str(order.side), reference_price=ref_px, fill_price=fill_px)
    return {
        "timestamp": order.timestamp.isoformat() if order.timestamp else None,
        "symbol": order.symbol,
        "broker": order.broker,
        "side": order.side,
        "status": order.status,
        "filled": filled_qty > 0 and fill_px is not None,
        "filled_quantity": float(filled_qty),
        "reference_price": float(ref_px) if ref_px is not None else None,
        "avg_fill_price": float(fill_px) if fill_px is not None else None,
        "realized_slippage_bps": slip,
        "fee": float(order.fee) if order.fee is not None else None,
        "microstructure_shadow_label": md.get("microstructure_shadow_label"),
        "microstructure_shadow_risk": md.get("microstructure_shadow_risk"),
        "microstructure_shadow_reasons": md.get("microstructure_shadow_reasons"),
        "microstructure_spread_bps": md.get("microstructure_spread_bps"),
        "microstructure_vpin_proxy": md.get("microstructure_vpin_proxy"),
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
        rows = [r for r in (order_to_outcome_row(o) for o in orders) if r is not None]
        summary = summarize_outcomes(rows)
        print("Phase D execution outcome join:")
        print(f"  scanned_orders={len(orders)}")
        print(f"  shadow_rows={summary['rows']}")
        print("  by_label:")
        for label, bucket in summary["by_label"].items():
            print(
                "    "
                f"{label}: count={bucket['count']} fill_rate={bucket['fill_rate']:.3f} "
                f"avg_slippage_bps={bucket['avg_slippage_bps']:.3f} "
                f"samples={bucket['slippage_samples']} fee_sum={bucket['fee_sum']:.4f}"
            )
        if rows:
            print("\nRecent outcome rows:")
            for row in rows[:10]:
                print(
                    "  "
                    f"{row['timestamp']} {row['broker']}:{row['symbol']} {row['side']} "
                    f"label={row['microstructure_shadow_label']} risk={row['microstructure_shadow_risk']} "
                    f"slip_bps={row['realized_slippage_bps']} fee={row['fee']}"
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
