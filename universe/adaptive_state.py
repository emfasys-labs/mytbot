"""D117 — Persisted adaptive universe-caps state.

The pipeline runner writes a small JSON file each cycle so that:

1. The dashboard / API (``universe/snapshot_service.py``) can show the
   *resolved* caps and the reasons behind them without re-running the
   policy.
2. The churn-hysteresis policy can carry ``consecutive_misses`` across
   rebuilds (otherwise we'd lose the budget every process restart).

The file is intentionally small and human-inspectable; corruption is
treated as "no prior state" so a single bad write cannot brick the
pipeline.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


DEFAULT_STATE_PATH = Path("data/runtime/universe_adaptive_state.json")


@dataclass
class AdaptiveRuntimeState:
    """In-memory mirror of ``data/runtime/universe_adaptive_state.json``."""

    updated_at: str = ""
    enabled: bool = False
    resolved: dict[str, Any] = field(default_factory=dict)
    context: dict[str, Any] = field(default_factory=dict)
    consecutive_misses: dict[str, int] = field(default_factory=dict)
    last_grace_extended: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "updated_at": self.updated_at,
            "enabled": bool(self.enabled),
            "resolved": dict(self.resolved),
            "context": dict(self.context),
            "consecutive_misses": dict(self.consecutive_misses),
            "last_grace_extended": list(self.last_grace_extended),
        }


def load_adaptive_state(path: Path | None = None) -> AdaptiveRuntimeState:
    """Read the persisted state file. Returns an empty state on any error."""
    p = path or DEFAULT_STATE_PATH
    if not p.is_file():
        return AdaptiveRuntimeState()
    try:
        blob = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return AdaptiveRuntimeState()
    if not isinstance(blob, Mapping):
        return AdaptiveRuntimeState()
    misses = blob.get("consecutive_misses")
    if isinstance(misses, Mapping):
        coerced_misses = {
            str(k).upper(): int(v)
            for k, v in misses.items()
            if str(k).strip() and isinstance(v, (int, float)) and v >= 0
        }
    else:
        coerced_misses = {}
    return AdaptiveRuntimeState(
        updated_at=str(blob.get("updated_at") or ""),
        enabled=bool(blob.get("enabled", False)),
        resolved=dict(blob.get("resolved") or {}),
        context=dict(blob.get("context") or {}),
        consecutive_misses=coerced_misses,
        last_grace_extended=list(blob.get("last_grace_extended") or []),
    )


def save_adaptive_state(state: AdaptiveRuntimeState, *, path: Path | None = None) -> Path:
    """Atomically write the state file (so a crashed write cannot corrupt prior state)."""
    p = path or DEFAULT_STATE_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    state.updated_at = datetime.now(timezone.utc).isoformat()
    payload = json.dumps(state.to_dict(), indent=2, sort_keys=True)
    tmp = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        suffix=".tmp",
        dir=str(p.parent),
        delete=False,
    )
    try:
        tmp.write(payload)
        tmp.flush()
        os.fsync(tmp.fileno())
    finally:
        tmp.close()
    os.replace(tmp.name, str(p))
    return p
