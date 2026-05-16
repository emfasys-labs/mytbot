"""
Evaluate Phase C transition shadow history against realised panel stress.

Read-only. Uses rows written to ``control_state`` key
``regime_transition.shadow_history`` and compares each warning with the future
cross-asset panel return over a configurable horizon.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timedelta, timezone
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

from control.command_bus import CommandBus  # noqa: E402
from storage.db import dispose_engine, init_async_database  # noqa: E402
from storage.models import FeatureSnapshot  # noqa: E402
from system.dashboard_publish import REGIME_TRANSITION_SHADOW_HISTORY_KEY  # noqa: E402

DEFAULT_PANEL = [
    "SPY",
    "QQQ",
    "IWM",
    "TLT",
    "HYG",
    "GLD",
    "BTC-USD",
    "ETH-USD",
    "SOL-USD",
    "NVDA",
    "AAPL",
    "MSFT",
    "USDCAD=X",
    "EURUSD=X",
]


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Evaluate Phase C transition shadow history")
    p.add_argument("--timeframe", default="1h")
    p.add_argument("--horizon-hours", type=int, default=4)
    p.add_argument("--stress-return", type=float, default=-0.004, help="panel return <= this is stress")
    p.add_argument("--limit", type=int, default=250)
    p.add_argument("--symbols", default=",".join(DEFAULT_PANEL))
    return p.parse_args()


def _parse_ts(raw: Any) -> datetime | None:
    if not raw:
        return None
    try:
        text = str(raw)
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def score_transition_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    evaluated = [r for r in rows if r.get("actual_stress") is not None and r.get("predicted_stress") is not None]
    tp = sum(1 for r in evaluated if r["predicted_stress"] and r["actual_stress"])
    fp = sum(1 for r in evaluated if r["predicted_stress"] and not r["actual_stress"])
    tn = sum(1 for r in evaluated if not r["predicted_stress"] and not r["actual_stress"])
    fn = sum(1 for r in evaluated if not r["predicted_stress"] and r["actual_stress"])
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    accuracy = (tp + tn) / len(evaluated) if evaluated else 0.0
    return {
        "evaluated": len(evaluated),
        "tp": tp,
        "fp": fp,
        "tn": tn,
        "fn": fn,
        "precision": precision,
        "recall": recall,
        "accuracy": accuracy,
    }


async def _panel_return(factory, *, ts: datetime, horizon: timedelta, symbols: list[str], timeframe: str) -> float | None:
    end = ts + horizon
    returns: list[float] = []
    async with factory() as session:
        for symbol in symbols:
            q0 = await session.execute(
                select(FeatureSnapshot.close)
                .where(
                    FeatureSnapshot.symbol == symbol,
                    FeatureSnapshot.timeframe == timeframe,
                    FeatureSnapshot.bar_timestamp <= ts,
                )
                .order_by(FeatureSnapshot.bar_timestamp.desc())
                .limit(1)
            )
            q1 = await session.execute(
                select(FeatureSnapshot.close)
                .where(
                    FeatureSnapshot.symbol == symbol,
                    FeatureSnapshot.timeframe == timeframe,
                    FeatureSnapshot.bar_timestamp >= end,
                )
                .order_by(FeatureSnapshot.bar_timestamp.asc())
                .limit(1)
            )
            p0 = q0.scalar_one_or_none()
            p1 = q1.scalar_one_or_none()
            if p0 is None or p1 is None:
                continue
            start = Decimal(str(p0))
            finish = Decimal(str(p1))
            if start > 0:
                returns.append(float((finish - start) / start))
    if not returns:
        return None
    return sum(returns) / len(returns)


async def _run(args: argparse.Namespace) -> int:
    engine, factory = await init_async_database()
    if engine is None or factory is None:
        print("No database configured.")
        return 2
    try:
        bus = CommandBus(factory)
        raw_history = await bus.get_state(REGIME_TRANSITION_SHADOW_HISTORY_KEY, [])
        history = raw_history if isinstance(raw_history, list) else []
        rows = [r for r in history[-max(1, int(args.limit)) :] if isinstance(r, dict)]
        symbols = [s.strip() for s in str(args.symbols).split(",") if s.strip()]
        horizon = timedelta(hours=max(1, int(args.horizon_hours)))
        scored: list[dict[str, Any]] = []
        for row in rows:
            ts = _parse_ts(row.get("timestamp"))
            if ts is None:
                continue
            realized = await _panel_return(factory, ts=ts, horizon=horizon, symbols=symbols, timeframe=args.timeframe)
            predicted = bool(row.get("label") == "stress_transition")
            actual = None if realized is None else bool(realized <= float(args.stress_return))
            scored.append(
                {
                    **row,
                    "predicted_stress": predicted,
                    "actual_stress": actual,
                    "future_panel_return": realized,
                }
            )
        summary = score_transition_rows(scored)
        print("Phase C transition calibration:")
        print(f"  history_rows={len(rows)}")
        print(f"  evaluated={summary['evaluated']}")
        print(f"  horizon_hours={args.horizon_hours}")
        print(f"  stress_return={args.stress_return}")
        print(
            "  "
            f"tp={summary['tp']} fp={summary['fp']} tn={summary['tn']} fn={summary['fn']} "
            f"precision={summary['precision']:.3f} recall={summary['recall']:.3f} "
            f"accuracy={summary['accuracy']:.3f}"
        )
        if scored:
            print("\nRecent scored rows:")
            for row in scored[-5:]:
                ret = row.get("future_panel_return")
                ret_s = "n/a" if ret is None else f"{ret:.5f}"
                print(
                    "  "
                    f"{row.get('timestamp')} label={row.get('label')} "
                    f"prob={row.get('probability')} future_panel_return={ret_s} "
                    f"actual_stress={row.get('actual_stress')}"
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
