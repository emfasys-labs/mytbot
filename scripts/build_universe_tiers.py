#!/usr/bin/env python3
"""
Offline build: correlation clusters + universe_intelligence.json.

Requires ``config/universe_selection.enabled: true`` and existing
``data/runtime/universe_tiers.json`` (from dynamic ranking / loop).

Does not place orders or touch RiskEngine.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from data.universe_tiers import load_universe_tiers  # noqa: E402
from universe.intelligence_builder import build_and_save_universe_intelligence  # noqa: E402
from universe.snapshot_service import load_universe_selection_config  # noqa: E402


async def _run() -> int:
    cfg = load_universe_selection_config(ROOT / "config" / "universe_selection.yaml")
    if not cfg.get("enabled"):
        print("universe_selection.enabled is false — enable in config/universe_selection.yaml to build.")
        return 0

    tiers = load_universe_tiers(ROOT / "data" / "runtime" / "universe_tiers.json")
    if not tiers:
        print("Missing data/runtime/universe_tiers.json — run the trading loop / ranking first.")
        return 1

    out_path = ROOT / str((cfg.get("persistence") or {}).get("intelligence_json", "data/runtime/universe_intelligence.json"))
    result = await build_and_save_universe_intelligence(tiers, cfg=cfg, output_path=out_path)
    if not result.wrote:
        print(f"Universe intelligence build skipped: {result.reason}")
        return 1
    print(f"wrote {result.path} | clusters={result.clusters} symbols_scored={result.symbols_scored}")
    return 0


def main() -> int:
    import asyncio

    return asyncio.run(_run())


if __name__ == "__main__":
    raise SystemExit(main())
