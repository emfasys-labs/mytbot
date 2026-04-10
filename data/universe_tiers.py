from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_TIERS_PATH = Path("data/runtime/universe_tiers.json")


@dataclass(frozen=True)
class UniverseTiers:
    """Broker-derived universe split for world-monitor style processing."""

    core: tuple[str, ...]
    scan: tuple[str, ...]
    light: tuple[str, ...]
    scores: dict[str, float]
    updated_at: str

    @property
    def all_ranked(self) -> tuple[str, ...]:
        return self.core + self.scan + self.light


def assign_tiers(
    scored: list[tuple[str, float]],
    *,
    core_max: int,
    scan_max: int,
) -> tuple[list[str], list[str], list[str]]:
    """Return (core, scan, light) from (symbol, score) pairs; higher score first."""
    ordered = sorted(scored, key=lambda x: x[1], reverse=True)
    syms = [s for s, _ in ordered]
    core = syms[:core_max]
    scan = syms[core_max : core_max + scan_max]
    light = syms[core_max + scan_max :]
    return core, scan, light


def save_universe_tiers(
    tiers: UniverseTiers,
    path: Path | None = None,
) -> None:
    p = path or DEFAULT_TIERS_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "core": list(tiers.core),
        "scan": list(tiers.scan),
        "light": list(tiers.light),
        "scores": tiers.scores,
        "updated_at": tiers.updated_at,
    }
    p.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def load_universe_tiers(path: Path | None = None) -> UniverseTiers | None:
    p = path or DEFAULT_TIERS_PATH
    if not p.is_file():
        return None
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    core = tuple(str(x).strip().upper() for x in raw.get("core", []) if str(x).strip())
    scan = tuple(str(x).strip().upper() for x in raw.get("scan", []) if str(x).strip())
    light = tuple(str(x).strip().upper() for x in raw.get("light", []) if str(x).strip())
    scores_raw = raw.get("scores") or {}
    scores = {str(k).strip().upper(): float(v) for k, v in scores_raw.items() if str(k).strip()}
    updated_at = str(raw.get("updated_at") or datetime.now(timezone.utc).isoformat())
    return UniverseTiers(core=core, scan=scan, light=light, scores=scores, updated_at=updated_at)
