"""In-memory / serialisable context for replacement interval + churn (D015)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from core.models_runtime import clip_decimal


@dataclass
class ReplacementContext:
    """Loaded once per trading loop iteration from ControlState or empty."""

    last_event_at_by_symbol: dict[str, datetime] = field(default_factory=dict)
    recent_events: list[dict[str, Any]] = field(default_factory=list)
    # Per-symbol timestamp of the most recent reduce-only CULL (capital-recycle
    # dead-edge close / adaptive-shed). Used to debounce the build-up/open path
    # so a symbol the recycle path just culled is not immediately re-opened
    # next iteration — the close→reopen loop that otherwise bleeds spread+fees.
    # Distinct from ``last_event_at_by_symbol`` (which also records opens) so a
    # normal recent open does not block a legitimate top-up.
    last_cull_at_by_symbol: dict[str, datetime] = field(default_factory=dict)

    @staticmethod
    def from_control_value(raw: object | None) -> ReplacementContext:
        if not isinstance(raw, dict):
            return ReplacementContext()
        evs = raw.get("recent_events")
        last_raw = raw.get("last_event_at_by_symbol") or {}
        last: dict[str, datetime] = {}
        if isinstance(last_raw, dict):
            for k, v in last_raw.items():
                try:
                    if isinstance(v, str):
                        last[str(k)] = datetime.fromisoformat(v.replace("Z", "+00:00"))
                except Exception:  # noqa: BLE001
                    continue
        cull_raw = raw.get("last_cull_at_by_symbol") or {}
        cull: dict[str, datetime] = {}
        if isinstance(cull_raw, dict):
            for k, v in cull_raw.items():
                try:
                    if isinstance(v, str):
                        cull[str(k)] = datetime.fromisoformat(v.replace("Z", "+00:00"))
                except Exception:  # noqa: BLE001
                    continue
        recent: list[dict[str, Any]] = []
        if isinstance(evs, list):
            for e in evs[-50:]:
                if isinstance(e, dict):
                    recent.append(dict(e))
        return ReplacementContext(
            last_event_at_by_symbol=last,
            recent_events=recent,
            last_cull_at_by_symbol=cull,
        )

    def to_control_value(self) -> dict[str, Any]:
        return {
            "last_event_at_by_symbol": {
                k: v.astimezone(timezone.utc).isoformat() for k, v in self.last_event_at_by_symbol.items()
            },
            "last_cull_at_by_symbol": {
                k: v.astimezone(timezone.utc).isoformat() for k, v in self.last_cull_at_by_symbol.items()
            },
            "recent_events": self.recent_events[-50:],
        }


def churn_penalty_for_pair(
    old_symbol: str,
    new_symbol: str,
    *,
    recent_events: list[dict[str, Any]],
    max_events: int,
    penalty_per_event: Decimal,
) -> Decimal:
    """Extra 0..1 penalty from recent flip-flop between same symbols."""
    if not recent_events or penalty_per_event <= 0:
        return Decimal("0")
    o = old_symbol.strip().upper()
    n = new_symbol.strip().upper()
    hits = 0
    for e in recent_events[-max_events:]:
        if not isinstance(e, dict):
            continue
        if str(e.get("old", "")).strip().upper() == n and str(e.get("new", "")).strip().upper() == o:
            hits += 1
    return clip_decimal(Decimal(str(hits)) * penalty_per_event, Decimal("0"), Decimal("1"))
