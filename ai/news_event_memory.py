"""
ai/news_event_memory.py
=========================
Wave 7 — short-term materiality-weighted memory of news events.

The signal accumulator (``signals/accumulator.py``) already maintains
a time-decayed conviction per symbol. ``NewsEventMemory`` is a sibling
specifically for *event-shaped* news (earnings beats, guidance,
M&A, macro shocks) where the bar matters most for the next few hours
and decays away after.

Key properties:

- Half-life decay (default 4h) — recent events dominate.
- Materiality weight (caller-supplied) — minor headlines count less.
- Bounded memory size — drops oldest events past ``max_events``.
- ``aggregate_score(symbol)`` returns a single signed scalar in roughly
  ``[-1, 1]`` for fusion ingestion.

Pure data structure — no IO, no broker access. Intentionally narrow so
it composes with ``ai/fusion.py``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


@dataclass(frozen=True)
class NewsEvent:
    symbol: str
    timestamp: datetime
    score: float          # signed score in roughly [-1, 1]
    materiality: float    # 0..1; higher = more impactful
    event_type: str = "news"
    rationale: str = ""

    @property
    def signed_weighted(self) -> float:
        return float(self.score) * float(max(0.0, min(1.0, self.materiality)))


@dataclass
class NewsEventMemory:
    half_life_seconds: float = 4 * 3600.0  # 4 hours
    max_events: int = 256
    _events: list[NewsEvent] = field(default_factory=list)

    # ── ingestion ───────────────────────────────────────────────────────────

    def record(self, event: NewsEvent) -> None:
        self._events.append(event)
        if len(self._events) > self.max_events:
            self._events = self._events[-self.max_events:]

    def record_many(self, events: list[NewsEvent]) -> None:
        for e in events:
            self.record(e)

    # ── decay ───────────────────────────────────────────────────────────────

    def _decay_factor(self, age_seconds: float) -> float:
        if age_seconds <= 0:
            return 1.0
        if self.half_life_seconds <= 0:
            return 0.0
        return float(0.5 ** (age_seconds / self.half_life_seconds))

    # ── reads ───────────────────────────────────────────────────────────────

    def recent_for_symbol(
        self,
        symbol: str,
        *,
        now: Optional[datetime] = None,
        lookback_seconds: Optional[float] = None,
    ) -> list[NewsEvent]:
        sym = (symbol or "").strip().upper()
        ref_now = now or datetime.now(timezone.utc)
        out: list[NewsEvent] = []
        for e in self._events:
            if (e.symbol or "").strip().upper() != sym:
                continue
            if lookback_seconds is not None:
                age = (ref_now - e.timestamp).total_seconds()
                if age > lookback_seconds:
                    continue
            out.append(e)
        return out

    def aggregate_score(
        self,
        symbol: str,
        *,
        now: Optional[datetime] = None,
        lookback_seconds: Optional[float] = None,
    ) -> float:
        """
        Decay-weighted sum of ``signed_weighted`` over the matching
        events, divided by the decay-weighted total materiality so the
        result lives in roughly ``[-1, 1]``.
        """
        ref_now = now or datetime.now(timezone.utc)
        events = self.recent_for_symbol(symbol, now=ref_now, lookback_seconds=lookback_seconds)
        if not events:
            return 0.0
        num = 0.0
        den = 0.0
        for e in events:
            age = (ref_now - e.timestamp).total_seconds()
            d = self._decay_factor(age)
            num += d * e.signed_weighted
            den += d * float(max(1e-9, e.materiality))
        if den <= 0:
            return 0.0
        # Soft clamp to [-1, 1].
        return float(max(-1.0, min(1.0, num / den)))

    def latest_materiality(
        self,
        symbol: str,
        *,
        now: Optional[datetime] = None,
        lookback_seconds: Optional[float] = None,
    ) -> float:
        """Highest decay-weighted materiality across events for this symbol."""
        ref_now = now or datetime.now(timezone.utc)
        events = self.recent_for_symbol(symbol, now=ref_now, lookback_seconds=lookback_seconds)
        if not events:
            return 0.0
        return float(
            max(
                self._decay_factor((ref_now - e.timestamp).total_seconds())
                * max(0.0, min(1.0, e.materiality))
                for e in events
            )
        )

    def __len__(self) -> int:
        return len(self._events)
