"""Export Phase C shadow history from control_state to report files."""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import sys
from datetime import datetime, timezone
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
from storage.db import dispose_engine, init_async_database  # noqa: E402
from system.dashboard_publish import REGIME_TRANSITION_SHADOW_HISTORY_KEY  # noqa: E402

DEFAULT_OUT_DIR = ROOT / "reports" / "models" / "phase_c_regime_transition"

FIELDS = [
    "timestamp",
    "loop_iteration",
    "path",
    "regime_label",
    "market_state_score",
    "breadth_score",
    "used",
    "shadow_only",
    "label",
    "probability",
    "threshold",
    "model_version",
    "reason",
    "policy_shadow_enabled",
    "policy_trigger_probability",
    "policy_exposure_multiplier",
    "policy_throttle_applied",
    "policy_reason",
]


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Export Phase C shadow history")
    p.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    p.add_argument("--prefix", default=None)
    return p.parse_args()


def write_shadow_history(rows: list[dict[str, Any]], *, out_dir: Path, prefix: str | None = None) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = prefix or f"phase_c_shadow_history_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    csv_path = out_dir / f"{stem}.csv"
    json_path = out_dir / f"{stem}.json"
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    json_path.write_text(json.dumps(rows, indent=2, default=str), encoding="utf-8")
    return csv_path, json_path


async def _run(args: argparse.Namespace) -> int:
    engine, factory = await init_async_database()
    if engine is None or factory is None:
        print("No database configured.")
        return 2
    try:
        bus = CommandBus(factory)
        raw = await bus.get_state(REGIME_TRANSITION_SHADOW_HISTORY_KEY, [])
        rows = [r for r in raw if isinstance(r, dict)] if isinstance(raw, list) else []
        csv_path, json_path = write_shadow_history(rows, out_dir=Path(args.out_dir), prefix=args.prefix)
        print("Phase C shadow history exported:")
        print(f"  rows={len(rows)}")
        print(f"  csv={csv_path}")
        print(f"  json={json_path}")
        return 0
    finally:
        await dispose_engine(engine)


def main() -> int:
    if load_dotenv is not None:
        load_dotenv(ROOT / ".env")
    return asyncio.run(_run(_parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
