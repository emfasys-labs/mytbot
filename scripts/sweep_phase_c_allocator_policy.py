"""
Sweep Phase C allocator throttle policies over shadow history.

Read-only. Evaluates combinations of trigger probability and throttle multiplier
using the same future panel return lookup as the D094 simulator.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import timedelta
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
from scripts.evaluate_phase_c_transition_history import DEFAULT_PANEL, _panel_return, _parse_ts  # noqa: E402
from scripts.simulate_phase_c_allocator_impact import simulate_allocator_impact  # noqa: E402
from storage.db import dispose_engine, init_async_database  # noqa: E402
from system.dashboard_publish import REGIME_TRANSITION_SHADOW_HISTORY_KEY  # noqa: E402


def _csv_floats(raw: str) -> list[float]:
    out: list[float] = []
    for part in str(raw).split(","):
        text = part.strip()
        if not text:
            continue
        out.append(float(text))
    return out


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Sweep Phase C allocator throttle policies")
    p.add_argument("--timeframe", default="1h")
    p.add_argument("--horizon-hours", type=int, default=4)
    p.add_argument("--limit", type=int, default=250)
    p.add_argument("--symbols", default=",".join(DEFAULT_PANEL))
    p.add_argument("--trigger-probabilities", default="0.45,0.50,0.55,0.60,0.65")
    p.add_argument("--throttle-multipliers", default="0.25,0.50,0.75")
    return p.parse_args()


def sweep_allocator_policies(
    rows: list[dict[str, Any]],
    *,
    trigger_probabilities: list[float],
    throttle_multipliers: list[float],
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for trigger in trigger_probabilities:
        for multiplier in throttle_multipliers:
            summary = simulate_allocator_impact(
                rows,
                trigger_probability=trigger,
                throttle_multiplier=multiplier,
            )
            results.append(summary)
    return sorted(
        results,
        key=lambda r: (
            float(r.get("impact_return_sum", 0.0)),
            float(r.get("avoided_loss", 0.0)),
            -float(r.get("missed_gain", 0.0)),
        ),
        reverse=True,
    )


async def _load_enriched_rows(args: argparse.Namespace) -> list[dict[str, Any]]:
    engine, factory = await init_async_database()
    if engine is None or factory is None:
        return []
    try:
        bus = CommandBus(factory)
        raw_history = await bus.get_state(REGIME_TRANSITION_SHADOW_HISTORY_KEY, [])
        history = raw_history if isinstance(raw_history, list) else []
        rows = [r for r in history[-max(1, int(args.limit)) :] if isinstance(r, dict)]
        symbols = [s.strip() for s in str(args.symbols).split(",") if s.strip()]
        horizon = timedelta(hours=max(1, int(args.horizon_hours)))
        enriched: list[dict[str, Any]] = []
        for row in rows:
            ts = _parse_ts(row.get("timestamp"))
            if ts is None:
                continue
            realized = await _panel_return(factory, ts=ts, horizon=horizon, symbols=symbols, timeframe=args.timeframe)
            enriched.append({**row, "future_panel_return": realized})
        return enriched
    finally:
        await dispose_engine(engine)


def _print_results(results: list[dict[str, Any]], *, history_rows: int, horizon_hours: int) -> None:
    print("Phase C allocator policy sweep:")
    print(f"  history_rows={history_rows}")
    print(f"  horizon_hours={horizon_hours}")
    evaluated = int(results[0]["evaluated"]) if results else 0
    print(f"  evaluated={evaluated}")
    if not results:
        print("  no policies evaluated")
        return
    print("\nTop policies:")
    for r in results[:10]:
        print(
            "  "
            f"trigger={r['trigger_probability']:.2f} "
            f"mult={r['throttle_multiplier']:.2f} "
            f"throttled={r['throttled']} "
            f"impact={r['impact_return_sum']:.6f} "
            f"avoided_loss={r['avoided_loss']:.6f} "
            f"missed_gain={r['missed_gain']:.6f}"
        )


def main() -> int:
    if load_dotenv is not None:
        load_dotenv(ROOT / ".env")
    args = _parse_args()
    rows = asyncio.run(_load_enriched_rows(args))
    results = sweep_allocator_policies(
        rows,
        trigger_probabilities=_csv_floats(args.trigger_probabilities),
        throttle_multipliers=_csv_floats(args.throttle_multipliers),
    )
    _print_results(results, history_rows=len(rows), horizon_hours=args.horizon_hours)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
