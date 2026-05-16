"""
Operator report for the Phase C regime-transition shadow detector.

Read-only. It prints the latest dashboard snapshot transition block plus the
configured artefact path so the operator can quickly see whether Phase C is
shadowing and what it most recently predicted.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
except Exception:  # noqa: BLE001
    load_dotenv = None

from control.command_bus import CommandBus  # noqa: E402
from storage.db import dispose_engine, init_async_database  # noqa: E402
from system.dashboard_publish import DASHBOARD_SNAPSHOT_KEY, REGIME_TRANSITION_SHADOW_HISTORY_KEY  # noqa: E402


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="mytbot Phase C transition shadow report")
    p.add_argument("--config", default="config/regime_models.yaml")
    return p.parse_args()


def _load_transition_config(path: Path) -> dict[str, Any]:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        return {"error": f"missing config: {path}"}
    except yaml.YAMLError as exc:
        return {"error": f"invalid YAML: {exc}"}
    return dict(((raw.get("regime_models") or {}).get("transition_detector") or {}))


def _print_config(cfg: dict[str, Any]) -> None:
    print("Phase C transition config:")
    if "error" in cfg:
        print(f"  error={cfg['error']}")
        return
    print(f"  enabled={bool(cfg.get('enabled', False))}")
    print(f"  shadow_only={bool(cfg.get('shadow_only', True))}")
    print(f"  threshold={cfg.get('threshold', '(unset)')}")
    print(f"  artifact_path={cfg.get('artifact_path') or '(unset)'}")
    names = cfg.get("feature_names") or []
    print(f"  feature_count={len(names)}")


def _print_snapshot(snapshot: dict[str, Any] | None) -> None:
    print("\nLatest dashboard transition snapshot:")
    if not snapshot:
        print("  unavailable: dashboard.snapshot not written yet")
        return
    print(f"  updated_at={snapshot.get('updated_at')}")
    regime = snapshot.get("regime") if isinstance(snapshot.get("regime"), dict) else {}
    print(f"  regime_label={regime.get('regime_label')}")
    transition = regime.get("transition") if isinstance(regime.get("transition"), dict) else None
    if not transition:
        print("  transition=(not present in latest snapshot)")
        return
    print(f"  used={transition.get('used')}")
    print(f"  shadow_only={transition.get('shadow_only')}")
    print(f"  label={transition.get('label')}")
    print(f"  probability={transition.get('probability')}")
    print(f"  threshold={transition.get('threshold')}")
    print(f"  model_version={transition.get('model_version')}")
    if transition.get("reason"):
        print(f"  reason={transition.get('reason')}")


def _print_history(rows: list[Any]) -> None:
    print("\nRecent transition history:")
    if not rows:
        print("  unavailable: no shadow history rows written yet")
        return
    print(f"  rows={len(rows)}")
    for row in rows[-5:]:
        if not isinstance(row, dict):
            continue
        print(
            "  "
            f"{row.get('timestamp')} "
            f"iter={row.get('loop_iteration')} "
            f"regime={row.get('regime_label')} "
            f"label={row.get('label') or row.get('reason')} "
            f"prob={row.get('probability')} "
            f"thr={row.get('threshold')}"
        )


async def _read_shadow_state() -> tuple[dict[str, Any] | None, list[Any]]:
    engine, factory = await init_async_database()
    if engine is None or factory is None:
        return None, []
    try:
        bus = CommandBus(factory)
        raw_snapshot = await bus.get_state(DASHBOARD_SNAPSHOT_KEY, None)
        raw_history = await bus.get_state(REGIME_TRANSITION_SHADOW_HISTORY_KEY, [])
        snapshot = raw_snapshot if isinstance(raw_snapshot, dict) else None
        history = raw_history if isinstance(raw_history, list) else []
        return snapshot, history
    finally:
        await dispose_engine(engine)


def main() -> int:
    if load_dotenv is not None:
        load_dotenv(ROOT / ".env")
    args = _parse_args()
    _print_config(_load_transition_config(ROOT / args.config))
    snapshot, history = asyncio.run(_read_shadow_state())
    _print_snapshot(snapshot)
    _print_history(history)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
