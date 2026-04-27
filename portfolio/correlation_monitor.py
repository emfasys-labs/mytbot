"""
portfolio/correlation_monitor.py
==================================
Wave 4 — rolling correlation monitor.

Tracks the most recent correlation matrix across a portfolio universe
and emits a structured alert when correlation structure shifts beyond
a configurable threshold (Frobenius norm of ``ρ_t - ρ_{t-1}``).

Used by the allocator and the risk dashboard to detect "risk-off"
events where everything correlates at once.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable, Optional

import numpy as np

from portfolio.covariance import correlation_from_covariance, ledoit_wolf_shrinkage

logger = logging.getLogger(__name__)


@dataclass
class CorrelationSnapshot:
    timestamp: datetime
    symbols: tuple[str, ...]
    correlation: np.ndarray
    average_pairwise: float
    max_pairwise: float
    method: str = "ledoit_wolf"


@dataclass
class CorrelationShiftAlert:
    timestamp: datetime
    delta_norm: float
    threshold: float
    average_before: float
    average_after: float
    max_pairwise_after: float


@dataclass
class CorrelationMonitor:
    threshold: float = 0.25  # Frobenius norm of (ρ_t - ρ_{t-1}) above this ⇒ alert
    min_samples: int = 30
    history: list[CorrelationSnapshot] = field(default_factory=list)
    history_limit: int = 32

    def update(
        self,
        *,
        symbols: Iterable[str],
        returns_matrix: np.ndarray,
        timestamp: Optional[datetime] = None,
    ) -> tuple[Optional[CorrelationSnapshot], Optional[CorrelationShiftAlert]]:
        """
        Compute a new correlation snapshot and return ``(snapshot, alert)``.

        ``alert`` is ``None`` when no shift exceeds the threshold.
        ``snapshot`` is ``None`` when the input is too small (returns
        the previous snapshot's view via ``self.latest()``).
        """
        ts = timestamp or datetime.now(timezone.utc)
        syms = tuple(symbols)
        R = np.asarray(returns_matrix, dtype=float)
        if R.ndim == 1:
            R = R.reshape(-1, 1)
        if R.shape[0] < self.min_samples or R.shape[1] != len(syms):
            return None, None

        cov = ledoit_wolf_shrinkage(R)
        rho = correlation_from_covariance(cov.matrix)

        p = rho.shape[0]
        if p > 1:
            mask = ~np.eye(p, dtype=bool)
            avg = float(rho[mask].mean())
            mx = float(rho[mask].max())
        else:
            avg = 1.0
            mx = 1.0

        snap = CorrelationSnapshot(
            timestamp=ts,
            symbols=syms,
            correlation=rho,
            average_pairwise=avg,
            max_pairwise=mx,
            method="ledoit_wolf",
        )

        alert: Optional[CorrelationShiftAlert] = None
        if self.history:
            prev = self.history[-1]
            if prev.symbols == syms and prev.correlation.shape == rho.shape:
                delta = float(np.linalg.norm(rho - prev.correlation, ord="fro"))
                if delta >= self.threshold:
                    alert = CorrelationShiftAlert(
                        timestamp=ts,
                        delta_norm=delta,
                        threshold=self.threshold,
                        average_before=prev.average_pairwise,
                        average_after=avg,
                        max_pairwise_after=mx,
                    )
                    logger.warning(
                        "correlation_monitor | shift detected | delta=%.4f thr=%.4f avg=%.3f→%.3f",
                        delta,
                        self.threshold,
                        prev.average_pairwise,
                        avg,
                    )

        self.history.append(snap)
        if len(self.history) > self.history_limit:
            self.history = self.history[-self.history_limit:]
        return snap, alert

    def latest(self) -> Optional[CorrelationSnapshot]:
        return self.history[-1] if self.history else None
