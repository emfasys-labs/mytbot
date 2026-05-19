"""D118 — Tier-transition ring buffer.

After each pipeline cycle the orchestrator diffs the previous and new
``(core, scan, light)`` tier membership and appends one row per moved
symbol. The dashboard surfaces the most recent N rows so the operator
can see the universe rotating in real time.

Storage is a single JSON file with a bounded ring buffer; oldest rows
fall off the end. Atomic writes (``tempfile + os.replace``) keep the
file safe against a crashed write. Corrupt loads return an empty list.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping


DEFAULT_TRANSITIONS_PATH = Path("data/runtime/universe_tier_transitions.json")
DEFAULT_RING_CAPACITY = 500

VALID_TIERS = frozenset({"core", "scan", "light", "absent"})


@dataclass(frozen=True)
class TierTransition:
    """One transition row."""

    ts: str
    symbol: str
    from_tier: str
    to_tier: str
    reason: str
    score_delta: float | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "ts": self.ts,
            "symbol": self.symbol,
            "from_tier": self.from_tier,
            "to_tier": self.to_tier,
            "reason": self.reason,
            "score_delta": (
                float(self.score_delta) if self.score_delta is not None else None
            ),
        }


@dataclass
class TransitionBuffer:
    """In-memory ring buffer of transition rows."""

    rows: list[TierTransition] = field(default_factory=list)
    capacity: int = DEFAULT_RING_CAPACITY

    def __len__(self) -> int:
        return len(self.rows)

    def append(self, row: TierTransition) -> None:
        self.rows.append(row)
        if len(self.rows) > self.capacity:
            self.rows = self.rows[-self.capacity:]

    def extend(self, rows: Iterable[TierTransition]) -> None:
        for row in rows:
            self.append(row)

    def recent(self, limit: int) -> list[TierTransition]:
        if limit <= 0:
            return []
        return list(self.rows[-limit:])

    def to_payload(self) -> dict[str, object]:
        return {
            "version": 1,
            "capacity": int(self.capacity),
            "rows": [r.to_dict() for r in self.rows],
        }


def _normalise(value: str) -> str:
    return str(value or "").strip().upper()


def _classify(prev_tier: str, new_tier: str) -> str:
    """Heuristic reason label for the UI filter chips."""
    if prev_tier == "absent" and new_tier in {"core", "scan"}:
        return "new_from_priority_rule"
    if prev_tier in {"core", "scan"} and new_tier == "light":
        return "demoted_to_light"
    if prev_tier == "light" and new_tier in {"core", "scan"}:
        return "promoted_to_watching"
    if prev_tier == "core" and new_tier == "scan":
        return "demoted_within_watching"
    if prev_tier == "scan" and new_tier == "core":
        return "promoted_within_watching"
    if new_tier == "absent":
        return "fell_off_universe"
    return f"{prev_tier}_to_{new_tier}"


def diff_tiers(
    *,
    previous: Mapping[str, str],
    new_core: Iterable[str],
    new_scan: Iterable[str],
    new_light: Iterable[str],
    scores_previous: Mapping[str, float] | None = None,
    scores_new: Mapping[str, float] | None = None,
    now: datetime | None = None,
) -> list[TierTransition]:
    """Diff previous vs new tier membership and return transition rows.

    ``previous`` is ``{symbol -> tier_name}`` for every symbol that was
    tracked last cycle; symbols absent from the new ``core/scan/light``
    sets are reported as ``to_tier="absent"``.
    """
    prev_map = {
        _normalise(sym): str(tier or "").strip().lower()
        for sym, tier in previous.items()
        if str(sym or "").strip()
    }
    new_map: dict[str, str] = {}
    for sym in new_core:
        new_map[_normalise(sym)] = "core"
    for sym in new_scan:
        new_map.setdefault(_normalise(sym), "scan")
    for sym in new_light:
        new_map.setdefault(_normalise(sym), "light")
    scores_prev = {_normalise(k): float(v) for k, v in (scores_previous or {}).items()}
    scores_new_map = {_normalise(k): float(v) for k, v in (scores_new or {}).items()}
    ts = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
    rows: list[TierTransition] = []
    for sym, prev_tier in prev_map.items():
        if not sym:
            continue
        new_tier = new_map.get(sym, "absent")
        if new_tier == prev_tier:
            continue
        score_prev = scores_prev.get(sym)
        score_new = scores_new_map.get(sym)
        delta: float | None = None
        if score_prev is not None and score_new is not None:
            delta = float(score_new - score_prev)
        rows.append(
            TierTransition(
                ts=ts,
                symbol=sym,
                from_tier=prev_tier or "absent",
                to_tier=new_tier,
                reason=_classify(prev_tier or "absent", new_tier),
                score_delta=delta,
            )
        )
    # Brand-new symbols (never in previous)
    for sym, new_tier in new_map.items():
        if sym in prev_map:
            continue
        rows.append(
            TierTransition(
                ts=ts,
                symbol=sym,
                from_tier="absent",
                to_tier=new_tier,
                reason=_classify("absent", new_tier),
                score_delta=None,
            )
        )
    rows.sort(key=lambda r: r.symbol)
    return rows


def build_previous_tier_map(
    *,
    core: Iterable[str],
    scan: Iterable[str],
    light: Iterable[str],
) -> dict[str, str]:
    """Construct the ``{symbol -> tier}`` map used by :func:`diff_tiers`."""
    out: dict[str, str] = {}
    for sym in core:
        out[_normalise(sym)] = "core"
    for sym in scan:
        out.setdefault(_normalise(sym), "scan")
    for sym in light:
        out.setdefault(_normalise(sym), "light")
    return out


def load_transitions(path: Path | None = None) -> TransitionBuffer:
    """Read the persisted ring buffer; returns empty buffer on any error."""
    p = path or DEFAULT_TRANSITIONS_PATH
    if not p.is_file():
        return TransitionBuffer()
    try:
        blob = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return TransitionBuffer()
    if not isinstance(blob, Mapping):
        return TransitionBuffer()
    rows_raw = blob.get("rows")
    capacity_raw = blob.get("capacity")
    capacity = int(capacity_raw) if isinstance(capacity_raw, (int, float)) else DEFAULT_RING_CAPACITY
    if not isinstance(rows_raw, list):
        return TransitionBuffer(capacity=capacity)
    rows: list[TierTransition] = []
    for entry in rows_raw:
        if not isinstance(entry, Mapping):
            continue
        try:
            sym = str(entry.get("symbol") or "").strip().upper()
            if not sym:
                continue
            from_tier = str(entry.get("from_tier") or "absent").strip().lower()
            to_tier = str(entry.get("to_tier") or "absent").strip().lower()
            delta_raw = entry.get("score_delta")
            delta = float(delta_raw) if delta_raw is not None else None
            rows.append(
                TierTransition(
                    ts=str(entry.get("ts") or ""),
                    symbol=sym,
                    from_tier=from_tier,
                    to_tier=to_tier,
                    reason=str(entry.get("reason") or ""),
                    score_delta=delta,
                )
            )
        except (TypeError, ValueError):
            continue
    return TransitionBuffer(rows=rows[-capacity:], capacity=capacity)


def save_transitions(buffer: TransitionBuffer, *, path: Path | None = None) -> Path:
    """Atomically write the ring buffer."""
    p = path or DEFAULT_TRANSITIONS_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(buffer.to_payload(), indent=2, sort_keys=True)
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


def record_transitions(
    rows: Iterable[TierTransition],
    *,
    path: Path | None = None,
    capacity: int = DEFAULT_RING_CAPACITY,
) -> TransitionBuffer:
    """Append rows to the persisted ring buffer in one step."""
    buf = load_transitions(path)
    if capacity > 0:
        buf.capacity = int(capacity)
    buf.extend(rows)
    save_transitions(buf, path=path)
    return buf
