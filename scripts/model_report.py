"""
scripts/model_report.py
========================
Wave 1 — operator-facing CLI that prints the registered model list and
recent prediction counts. Read-only; never mutates the registry or DB.

Usage:
    python scripts/model_report.py
    python scripts/model_report.py --name my_meta_labeler
    python scripts/model_report.py --since 2026-04-01

Without a database connection this prints only the YAML registry.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

# Ensure the repo root is on sys.path when invoked as a script.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from models.registry import ModelRegistry  # noqa: E402
from models.prediction_store import read_predictions  # noqa: E402


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="mytbot model registry report (Wave 1)")
    p.add_argument("--registry", default=None, help="path to model_registry.yaml")
    p.add_argument("--name", default=None, help="filter to a specific model name")
    p.add_argument(
        "--since",
        default=None,
        help="ISO-8601 timestamp; show predictions written after this time",
    )
    p.add_argument("--limit", type=int, default=20)
    return p.parse_args()


def _print_registry(registry: ModelRegistry, name_filter: str | None) -> None:
    names = registry.names()
    if name_filter:
        names = [n for n in names if n == name_filter]
    if not names:
        print("(no models registered)")
        return
    print("Registered models:")
    for n in names:
        for c in registry._by_name[n]:  # internal but acceptable for a report tool
            print(
                f"  {c.name}@{c.version}  task={c.task.value}  "
                f"target={c.target}  status={c.approval_status.value}  "
                f"calib={c.calibration_method}  fc={c.feature_contract_hash[:12]}"
            )


async def _print_recent_predictions(args: argparse.Namespace) -> None:
    try:
        from storage.db import init_async_database
    except Exception as exc:  # noqa: BLE001
        print(f"(skipping DB section: storage.db not importable — {exc})")
        return

    engine, factory = await init_async_database()
    if engine is None or factory is None:
        print("(skipping DB section: no database configured)")
        return

    since = None
    if args.since:
        since = datetime.fromisoformat(args.since)
        if since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)

    rows = await read_predictions(
        factory,
        model_name=args.name,
        since=since,
        limit=args.limit,
    )
    print(f"\nRecent predictions ({len(rows)}):")
    for r in rows:
        print(
            f"  {r['prediction_ts'].isoformat()}  "
            f"{r['model_name']}@{r['model_version']}  "
            f"{r['symbol']}  mode={r['mode']}  "
            f"prob={r['predicted_probability']}  "
            f"conf={r['confidence']}"
        )

    from storage.db import dispose_engine

    await dispose_engine(engine)


def main() -> int:
    args = _parse_args()
    registry = ModelRegistry.load(args.registry) if args.registry else ModelRegistry.load()
    _print_registry(registry, args.name)
    try:
        asyncio.run(_print_recent_predictions(args))
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
