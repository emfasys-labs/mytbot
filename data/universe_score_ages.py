"""D118 — Per-symbol score-age persistence.

Tracks ``last_scored_at`` and ``last_score`` for every symbol the
priority pre-filter has ever scored. This is read by
``data.universe_prefilter`` to compute the ``freshness_bonus`` and
``liquidity_prior`` components without any in-memory state.

Persistence is intentionally tiny and atomic (``tempfile + os.replace``)
so a crashed write cannot corrupt prior history. Corrupt JSON loads as
"no prior state" rather than crashing the pipeline.

Timeouts must NOT update the row — keeping the old ``last_scored_at``
makes the symbol's freshness bonus stay high and bubble it back to the
top of the next cycle naturally.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping


DEFAULT_SCORE_AGES_PATH = Path("data/runtime/universe_score_ages.json")
DEFAULT_MAX_AGES_KEPT = 25_000


@dataclass(frozen=True)
class ScoreAgeRow:
    """Persisted row for one symbol.

    ``last_scored_at`` is an ISO-8601 UTC string; ``None`` means the
    symbol has never been scored.
    """

    symbol: str
    last_scored_at: str | None
    last_score: float | None
    score_count: int
    first_seen_at: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "last_scored_at": self.last_scored_at,
            "last_score": self.last_score,
            "score_count": int(self.score_count),
            "first_seen_at": self.first_seen_at,
        }


class ScoreAges:
    """In-memory mirror of the score-ages JSON file.

    Designed to be loaded once per pipeline cycle, mutated as scoring
    completes, and persisted atomically at end of cycle.
    """

    def __init__(self, rows: Mapping[str, ScoreAgeRow] | None = None) -> None:
        self._rows: dict[str, ScoreAgeRow] = dict(rows or {})

    def __len__(self) -> int:
        return len(self._rows)

    def __contains__(self, symbol: str) -> bool:
        return self._normalise(symbol) in self._rows

    def get(self, symbol: str) -> ScoreAgeRow | None:
        return self._rows.get(self._normalise(symbol))

    def items(self) -> Iterable[tuple[str, ScoreAgeRow]]:
        return self._rows.items()

    def symbols(self) -> list[str]:
        return list(self._rows.keys())

    def age_seconds_for(self, symbol: str, *, now: datetime | None = None) -> float | None:
        """Seconds since the symbol's last successful score.

        Returns ``None`` for never-scored symbols so the caller can map
        them to the freshness ceiling explicitly.
        """
        row = self.get(symbol)
        if row is None or not row.last_scored_at:
            return None
        when = _parse_iso(row.last_scored_at)
        if when is None:
            return None
        now_dt = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        delta = (now_dt - when).total_seconds()
        return float(max(0.0, delta))

    def last_score_for(self, symbol: str) -> float | None:
        row = self.get(symbol)
        if row is None or row.last_score is None:
            return None
        return float(row.last_score)

    def record_scores(
        self,
        scored: Mapping[str, float],
        *,
        timeouts: Iterable[str] | None = None,
        now: datetime | None = None,
    ) -> None:
        """Update ``last_scored_at`` and ``last_score`` for completed symbols.

        ``timeouts`` lists symbols the scorer attempted but did not
        finish; they are intentionally NOT updated so their freshness
        bonus persists.
        """
        now_str = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
        timeout_set = {self._normalise(s) for s in (timeouts or [])}
        for sym, score in scored.items():
            key = self._normalise(sym)
            if not key or key in timeout_set:
                continue
            prev = self._rows.get(key)
            self._rows[key] = ScoreAgeRow(
                symbol=key,
                last_scored_at=now_str,
                last_score=float(score),
                score_count=(prev.score_count + 1) if prev is not None else 1,
                first_seen_at=(prev.first_seen_at if prev is not None else now_str),
            )

    def observe_unseen(
        self,
        symbols: Iterable[str],
        *,
        now: datetime | None = None,
    ) -> None:
        """Mark symbols as known-but-never-scored.

        Inserts a row with ``last_scored_at=None`` so the freshness
        bonus is at the ceiling and the pre-filter can rank them.
        Existing rows are left untouched.
        """
        now_str = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
        for sym in symbols:
            key = self._normalise(sym)
            if not key or key in self._rows:
                continue
            self._rows[key] = ScoreAgeRow(
                symbol=key,
                last_scored_at=None,
                last_score=None,
                score_count=0,
                first_seen_at=now_str,
            )

    def evict_to_cap(self, max_rows: int) -> int:
        """Cap the number of stored rows; evict oldest-touched first.

        Returns the number of rows evicted. Rows that have ``None``
        timestamps are evicted before any successfully-scored row so
        unscored-but-tracked symbols remain available for the pre-filter
        while real telemetry survives churn.
        """
        if max_rows <= 0 or len(self._rows) <= max_rows:
            return 0

        def _sort_key(item: tuple[str, ScoreAgeRow]) -> tuple[int, float]:
            _, row = item
            # Lower keep-priority key = keep this row first.
            # Group 0 = successfully scored (highest keep priority).
            # Group 1 = never scored.
            # Within each group, prefer the most recently touched row:
            # we negate the epoch so newer rows sort earlier.
            group = 0 if row.last_scored_at else 1
            ts_str = row.last_scored_at or row.first_seen_at or ""
            epoch = _to_epoch(ts_str)
            return (group, -epoch)

        sorted_rows = sorted(self._rows.items(), key=_sort_key)
        kept = dict(sorted_rows[:max_rows])
        evicted = len(self._rows) - len(kept)
        self._rows = kept
        return evicted

    def summary(self, *, now: datetime | None = None) -> dict[str, object]:
        """Aggregate statistics for the dashboard."""
        total = len(self._rows)
        never = 0
        ages: list[float] = []
        now_dt = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        for row in self._rows.values():
            if not row.last_scored_at:
                never += 1
                continue
            when = _parse_iso(row.last_scored_at)
            if when is None:
                never += 1
                continue
            age = max(0.0, (now_dt - when).total_seconds())
            ages.append(age)
        median_age = _median(ages) if ages else 0.0
        return {
            "total_tracked": int(total),
            "never_scored": int(never),
            "median_age_sec": float(median_age),
        }

    def to_payload(self) -> dict[str, object]:
        return {
            "version": 1,
            "rows": {sym: row.to_dict() for sym, row in self._rows.items()},
        }

    @staticmethod
    def _normalise(symbol: str) -> str:
        return str(symbol or "").strip().upper()


def _parse_iso(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _to_epoch(value: str) -> float:
    when = _parse_iso(value)
    if when is None:
        return 0.0
    return when.astimezone(timezone.utc).timestamp()


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2 == 1:
        return float(ordered[mid])
    return float((ordered[mid - 1] + ordered[mid]) / 2.0)


def load_score_ages(path: Path | None = None) -> ScoreAges:
    """Read the persisted score-age file; returns empty state on any error."""
    p = path or DEFAULT_SCORE_AGES_PATH
    if not p.is_file():
        return ScoreAges()
    try:
        blob = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ScoreAges()
    if not isinstance(blob, Mapping):
        return ScoreAges()
    raw_rows = blob.get("rows")
    if not isinstance(raw_rows, Mapping):
        return ScoreAges()
    rows: dict[str, ScoreAgeRow] = {}
    for sym, body in raw_rows.items():
        if not isinstance(body, Mapping):
            continue
        key = str(sym or "").strip().upper()
        if not key:
            continue
        try:
            score_raw = body.get("last_score")
            last_score = float(score_raw) if score_raw is not None else None
            score_count = int(body.get("score_count") or 0)
            last_scored_at = body.get("last_scored_at")
            first_seen_at = body.get("first_seen_at")
            rows[key] = ScoreAgeRow(
                symbol=key,
                last_scored_at=str(last_scored_at) if last_scored_at else None,
                last_score=last_score,
                score_count=max(0, score_count),
                first_seen_at=str(first_seen_at) if first_seen_at else None,
            )
        except (TypeError, ValueError):
            continue
    return ScoreAges(rows)


def save_score_ages(state: ScoreAges, *, path: Path | None = None) -> Path:
    """Atomically write the score-age file."""
    p = path or DEFAULT_SCORE_AGES_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(state.to_payload(), indent=2, sort_keys=True)
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
