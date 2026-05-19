"""D117 — Build the ``AdaptiveCapsContext`` from live system state.

The :mod:`universe.adaptive_caps` decision module is intentionally
pure; this thin async loader is the *only* place that touches I/O to
assemble its inputs from the running app.

Inputs we read:

- ``dashboard.snapshot`` from ``ControlState`` (written every loop tick
  by :mod:`system.dashboard_publish`) — gives us the most recent
  ``regime_label``, ``breadth_score``, and a signal-pressure proxy
  (``dashboard_feed.batch_candidate_count``).
- ``data/runtime/universe_intelligence.json`` — gives us the live
  correlation-cluster count so the cluster-aware floor can apply.

All reads are best-effort: any missing/invalid input produces a
neutral context (``regime_label='mixed'`` etc.) which the policy maps
to multiplier 1.0. The pipeline therefore always has a valid context
even if Postgres or the intelligence file are momentarily unavailable.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from loguru import logger
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from universe.adaptive_caps import AdaptiveCapsContext


DEFAULT_INTELLIGENCE_PATH = Path("data/runtime/universe_intelligence.json")


def _coerce_int(value: Any) -> int | None:
    try:
        if value is None:
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _coerce_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _coerce_str(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def _extract_regime(snapshot: Mapping[str, Any]) -> tuple[str | None, float | None]:
    """Pull regime_label + breadth_score from the dashboard snapshot."""
    regime = snapshot.get("regime")
    if not isinstance(regime, Mapping):
        return None, None
    label = _coerce_str(regime.get("regime_label"))
    breadth = _coerce_float(regime.get("breadth_score"))
    return label, breadth


def _extract_signal_pressure(snapshot: Mapping[str, Any]) -> int | None:
    """Recent signal-pressure proxy from the heartbeat feed."""
    feed = snapshot.get("dashboard_feed")
    if isinstance(feed, Mapping):
        n = _coerce_int(feed.get("batch_candidate_count"))
        if n is not None:
            return max(0, n)
    # Fallback: count opportunities the allocator currently sees.
    opps = snapshot.get("opportunities")
    if isinstance(opps, list):
        return max(0, len(opps))
    return None


def _extract_active_cluster_count(intelligence_path: Path) -> int | None:
    """Count clusters in ``universe_intelligence.json``.

    Falls back to ``None`` if the file is missing or malformed so the
    policy treats cluster-aware floor as unavailable instead of forcing
    it to a fabricated value.
    """
    if not intelligence_path.is_file():
        return None
    try:
        blob = json.loads(intelligence_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    clusters = blob.get("clusters") if isinstance(blob, Mapping) else None
    if isinstance(clusters, list):
        return len(clusters)
    return None


async def _load_dashboard_snapshot(
    session_factory: async_sessionmaker[AsyncSession],
) -> dict[str, Any] | None:
    """Read the latest ``dashboard.snapshot`` from ``ControlState``.

    Best-effort: failures are swallowed and the caller treats the result
    as "no snapshot available".
    """
    try:
        from control.command_bus import CommandBus
        from system.dashboard_publish import DASHBOARD_SNAPSHOT_KEY
    except Exception:  # noqa: BLE001
        return None
    try:
        bus = CommandBus(session_factory)
        raw = await bus.get_state(DASHBOARD_SNAPSHOT_KEY, None)
    except Exception as exc:  # noqa: BLE001
        logger.debug("adaptive_context | snapshot read failed: {}", exc)
        return None
    if isinstance(raw, Mapping):
        return dict(raw)
    return None


async def build_adaptive_caps_context(
    *,
    session_factory: async_sessionmaker[AsyncSession] | None,
    intelligence_path: Path | None = None,
) -> AdaptiveCapsContext:
    """Assemble the runtime context the policy needs.

    Safe defaults: when nothing is known we return ``regime_label='mixed'``,
    no signal pressure, no cluster count — which the policy maps to
    multiplier 1.0 and treats as a neutral tick.
    """
    label: str | None = None
    breadth: float | None = None
    pressure: int | None = None

    if session_factory is not None:
        snapshot = await _load_dashboard_snapshot(session_factory)
        if snapshot is not None:
            label, breadth = _extract_regime(snapshot)
            pressure = _extract_signal_pressure(snapshot)

    clusters = _extract_active_cluster_count(intelligence_path or DEFAULT_INTELLIGENCE_PATH)

    note_parts: list[str] = []
    if label is None:
        note_parts.append("regime=missing")
    if pressure is None:
        note_parts.append("signal_pressure=missing")
    if clusters is None:
        note_parts.append("clusters=missing")

    return AdaptiveCapsContext(
        regime_label=label or "mixed",
        breadth_score=breadth,
        signal_pressure=pressure,
        active_cluster_count=clusters,
        note=("; ".join(note_parts)) or None,
    )
