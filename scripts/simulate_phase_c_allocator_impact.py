"""
Simulate allocator impact from Phase C transition shadow predictions.

Read-only. This does not change live sizing. It asks a narrow question:
if exposure had been multiplied by ``--throttle-multiplier`` whenever Phase C
probability crossed ``--trigger-probability``, what would the next-horizon
panel return impact have been?
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
from storage.db import dispose_engine, init_async_database  # noqa: E402
from system.dashboard_publish import REGIME_TRANSITION_SHADOW_HISTORY_KEY  # noqa: E402


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Simulate Phase C allocator impact from shadow history")
    p.add_argument("--timeframe", default="1h")
    p.add_argument("--horizon-hours", type=int, default=4)
    p.add_argument("--limit", type=int, default=250)
    p.add_argument("--symbols", default=",".join(DEFAULT_PANEL))
    p.add_argument("--trigger-probability", type=float, default=0.55)
    p.add_argument("--throttle-multiplier", type=float, default=0.50)
    return p.parse_args()


def simulate_allocator_impact(
    rows: list[dict[str, Any]],
    *,
    trigger_probability: float = 0.55,
    throttle_multiplier: float = 0.50,
) -> dict[str, Any]:
    evaluated = [r for r in rows if r.get("future_panel_return") is not None and r.get("probability") is not None]
    baseline_return = 0.0
    simulated_return = 0.0
    throttled = 0
    avoided_loss = 0.0
    missed_gain = 0.0
    for row in evaluated:
        ret = float(row["future_panel_return"])
        prob = float(row["probability"])
        mult = float(throttle_multiplier) if prob >= float(trigger_probability) else 1.0
        baseline_return += ret
        simulated_return += ret * mult
        if mult < 1.0:
            throttled += 1
            delta = ret * mult - ret
            if delta > 0:
                avoided_loss += delta
            elif delta < 0:
                missed_gain += abs(delta)
    return {
        "evaluated": len(evaluated),
        "throttled": throttled,
        "trigger_probability": float(trigger_probability),
        "throttle_multiplier": float(throttle_multiplier),
        "baseline_return_sum": baseline_return,
        "simulated_return_sum": simulated_return,
        "impact_return_sum": simulated_return - baseline_return,
        "avoided_loss": avoided_loss,
        "missed_gain": missed_gain,
        "avg_baseline_return": baseline_return / len(evaluated) if evaluated else 0.0,
        "avg_simulated_return": simulated_return / len(evaluated) if evaluated else 0.0,
    }


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
        enriched: list[dict[str, Any]] = []
        for row in rows:
            ts = _parse_ts(row.get("timestamp"))
            if ts is None:
                continue
            realized = await _panel_return(factory, ts=ts, horizon=horizon, symbols=symbols, timeframe=args.timeframe)
            enriched.append({**row, "future_panel_return": realized})
        summary = simulate_allocator_impact(
            enriched,
            trigger_probability=float(args.trigger_probability),
            throttle_multiplier=float(args.throttle_multiplier),
        )
        print("Phase C allocator-impact simulation:")
        print(f"  history_rows={len(rows)}")
        print(f"  evaluated={summary['evaluated']}")
        print(f"  horizon_hours={args.horizon_hours}")
        print(f"  trigger_probability={summary['trigger_probability']}")
        print(f"  throttle_multiplier={summary['throttle_multiplier']}")
        print(f"  throttled={summary['throttled']}")
        print(f"  baseline_return_sum={summary['baseline_return_sum']:.6f}")
        print(f"  simulated_return_sum={summary['simulated_return_sum']:.6f}")
        print(f"  impact_return_sum={summary['impact_return_sum']:.6f}")
        print(f"  avoided_loss={summary['avoided_loss']:.6f}")
        print(f"  missed_gain={summary['missed_gain']:.6f}")
        if enriched:
            print("\nRecent simulated rows:")
            for row in enriched[-5:]:
                ret = row.get("future_panel_return")
                ret_s = "n/a" if ret is None else f"{float(ret):.5f}"
                print(
                    "  "
                    f"{row.get('timestamp')} prob={row.get('probability')} "
                    f"label={row.get('label')} future_panel_return={ret_s}"
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
