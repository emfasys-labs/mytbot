"""
system/funnel_telemetry.py
============================
Wave 13 — opportunity funnel counters.

Seven stages, monotonically narrowing:

    evaluated → generated → meta_label_kept → forecast_kept →
    risk_approved → execution_approved → executed

The dashboard renders the deltas so the operator can see *why* a
strategy generated zero trades — instead of an opaque "0 trades".
Every stage exposes a per-strategy bucket plus an aggregate.

Thread-safety: counters are guarded by a ``threading.Lock``. The
trading loop is async-single-threaded but background fill polls and
the API thread can both touch the registry.
"""

from __future__ import annotations

import threading
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


# ── data ───────────────────────────────────────────────────────────────────


@dataclass
class FunnelCounters:
    evaluated: Counter = field(default_factory=Counter)
    generated: Counter = field(default_factory=Counter)
    meta_label_kept: Counter = field(default_factory=Counter)
    meta_label_blocked: Counter = field(default_factory=Counter)
    forecast_kept: Counter = field(default_factory=Counter)
    forecast_blocked: Counter = field(default_factory=Counter)
    risk_approved: Counter = field(default_factory=Counter)
    risk_rejected: Counter = field(default_factory=Counter)
    execution_approved: Counter = field(default_factory=Counter)
    execution_blocked: Counter = field(default_factory=Counter)
    executed: Counter = field(default_factory=Counter)
    last_updated: Optional[datetime] = None

    def to_dict(self) -> dict:
        def _c(c: Counter) -> dict:
            return {str(k): int(v) for k, v in c.items()}

        per_strategy: dict[str, dict[str, int]] = {}
        all_stages = (
            ("evaluated", self.evaluated),
            ("generated", self.generated),
            ("meta_label_kept", self.meta_label_kept),
            ("meta_label_blocked", self.meta_label_blocked),
            ("forecast_kept", self.forecast_kept),
            ("forecast_blocked", self.forecast_blocked),
            ("risk_approved", self.risk_approved),
            ("risk_rejected", self.risk_rejected),
            ("execution_approved", self.execution_approved),
            ("execution_blocked", self.execution_blocked),
            ("executed", self.executed),
        )
        for stage, c in all_stages:
            for sym, n in c.items():
                per_strategy.setdefault(str(sym), {})[stage] = int(n)

        aggregate = {stage: int(sum(c.values())) for stage, c in all_stages}
        return {
            "aggregate": aggregate,
            "per_strategy": per_strategy,
            "last_updated": self.last_updated.isoformat() if self.last_updated else None,
        }


# ── thread-safe registry ───────────────────────────────────────────────────


class FunnelTelemetry:
    """Process-wide counter aggregator."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters = FunnelCounters()

    def record_evaluated(self, strategy: str, n: int = 1) -> None:
        self._bump("evaluated", strategy, n)

    def record_generated(self, strategy: str, n: int = 1) -> None:
        self._bump("generated", strategy, n)

    def record_meta_label_kept(self, strategy: str, n: int = 1) -> None:
        self._bump("meta_label_kept", strategy, n)

    def record_meta_label_blocked(self, strategy: str, n: int = 1) -> None:
        self._bump("meta_label_blocked", strategy, n)

    def record_forecast_kept(self, strategy: str, n: int = 1) -> None:
        self._bump("forecast_kept", strategy, n)

    def record_forecast_blocked(self, strategy: str, n: int = 1) -> None:
        self._bump("forecast_blocked", strategy, n)

    def record_risk_approved(self, strategy: str, n: int = 1) -> None:
        self._bump("risk_approved", strategy, n)

    def record_risk_rejected(self, strategy: str, n: int = 1) -> None:
        self._bump("risk_rejected", strategy, n)

    def record_execution_approved(self, strategy: str, n: int = 1) -> None:
        self._bump("execution_approved", strategy, n)

    def record_execution_blocked(self, strategy: str, n: int = 1) -> None:
        self._bump("execution_blocked", strategy, n)

    def record_executed(self, strategy: str, n: int = 1) -> None:
        self._bump("executed", strategy, n)

    def snapshot(self) -> FunnelCounters:
        with self._lock:
            # Return a copy so callers can serialise without races.
            c = self._counters
            return FunnelCounters(
                evaluated=Counter(c.evaluated),
                generated=Counter(c.generated),
                meta_label_kept=Counter(c.meta_label_kept),
                meta_label_blocked=Counter(c.meta_label_blocked),
                forecast_kept=Counter(c.forecast_kept),
                forecast_blocked=Counter(c.forecast_blocked),
                risk_approved=Counter(c.risk_approved),
                risk_rejected=Counter(c.risk_rejected),
                execution_approved=Counter(c.execution_approved),
                execution_blocked=Counter(c.execution_blocked),
                executed=Counter(c.executed),
                last_updated=c.last_updated,
            )

    def reset(self) -> None:
        with self._lock:
            self._counters = FunnelCounters()

    # ── internals ───────────────────────────────────────────────────────────

    def _bump(self, stage: str, strategy: str, n: int) -> None:
        if n <= 0:
            return
        key = (strategy or "unknown").strip() or "unknown"
        with self._lock:
            counter: Counter = getattr(self._counters, stage)
            counter[key] += int(n)
            self._counters.last_updated = datetime.now(timezone.utc)


# ── singleton ──────────────────────────────────────────────────────────────


_default = FunnelTelemetry()


def get_default_funnel_telemetry() -> FunnelTelemetry:
    return _default


def reset_default_funnel_telemetry() -> None:
    """Test helper."""
    _default.reset()


def record_strategy_candidate_rows(
    rows: list[dict],
    *,
    funnel: Optional[FunnelTelemetry] = None,
) -> None:
    """
    Bridge ``strategy_candidate_log`` rows into the Wave 13 funnel.

    The trading loop already records the strategy lifecycle in these row dicts.
    This helper keeps the dashboard counters aligned with that existing runtime
    source instead of adding a parallel per-strategy instrumentation path.
    """
    if not rows:
        return
    f = funnel or get_default_funnel_telemetry()
    evaluated_statuses = {
        "no_setup",
        "skipped",
        "filtered_regime",
        "filtered_signal_engine",
        "filtered_meta",
        "lost_to_strategy",
        "generated",
        "batched",
    }
    for r in rows:
        strategy = str(r.get("strategy") or "unknown").strip() or "unknown"
        status = str(r.get("status") or "").strip().lower()
        if status in evaluated_statuses:
            f.record_evaluated(strategy)
        if status in {"generated", "batched"}:
            f.record_generated(strategy)
        elif status == "filtered_meta":
            f.record_meta_label_blocked(strategy)
        elif status == "filtered_signal_engine":
            reason = str(r.get("reason") or "").lower()
            if "forecast" in reason:
                f.record_forecast_blocked(strategy)
