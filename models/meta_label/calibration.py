"""
models/meta_label/calibration.py
=================================
Reliability / calibration table for a trained probability model.

Each row records: ``predicted`` (model's calibrated probability for that
bin), ``observed`` (empirical win rate at that bin in validation),
``n`` (sample count). The table is the **only** legitimate source of
truth for "what probability threshold corresponds to what win rate" at
runtime — it replaces hardcoded threshold constants with a lookup
against historical evidence the model itself was validated on.

Public API:

* ``CalibrationTable.from_dict(...)`` — build from registry metadata.
* ``CalibrationTable.from_validation_markdown(path)`` — parse the
  validation report's ``Calibration`` block (fallback path when the
  registry entry has no embedded table).
* ``table.lowest_threshold_for(target_win_rate)`` — the smallest
  predicted-probability cut-point whose observed bin clears the target.
  Used by the dynamic threshold resolver.
* ``table.best_observed`` — ceiling used to clamp targets (never
  ask for more than the best-calibrated bin actually delivered).
* ``table.base_rate_estimate`` — sample-size-weighted observed mean,
  used as the floor.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Optional, Sequence

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CalibrationBin:
    predicted: float
    observed: float
    n: int


@dataclass(frozen=True)
class CalibrationTable:
    """Sorted (by predicted) calibration bins from validation."""

    bins: tuple[CalibrationBin, ...]

    # ── construction ───────────────────────────────────────────────────────

    @classmethod
    def from_bins(cls, bins: Iterable[CalibrationBin]) -> "CalibrationTable":
        ordered = tuple(sorted(bins, key=lambda b: b.predicted))
        return cls(bins=ordered)

    @classmethod
    def from_dict(cls, raw: Optional[Sequence[Mapping[str, object]]]) -> Optional["CalibrationTable"]:
        if not raw:
            return None
        try:
            bins = [
                CalibrationBin(
                    predicted=float(row["predicted"]),
                    observed=float(row["observed"]),
                    n=int(row.get("n", 0) or 0),
                )
                for row in raw
            ]
        except (KeyError, TypeError, ValueError) as exc:
            logger.warning("CalibrationTable.from_dict | malformed entry: %s", exc)
            return None
        if not bins:
            return None
        return cls.from_bins(bins)

    # Matches lines like:  "- Bin 0.25: predicted 0.294, observed 0.032, n=94"
    _BIN_LINE_RE = re.compile(
        r"-\s*Bin\s+([\d.]+):\s*predicted\s+([\d.]+),\s*observed\s+([\d.]+),\s*n\s*=\s*(\d+)",
        re.IGNORECASE,
    )

    @classmethod
    def from_validation_markdown(cls, path: Path | str) -> Optional["CalibrationTable"]:
        p = Path(path)
        if not p.exists():
            return None
        try:
            text = p.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning("CalibrationTable.from_validation_markdown | %s: %s", p, exc)
            return None
        bins: list[CalibrationBin] = []
        for m in cls._BIN_LINE_RE.finditer(text):
            try:
                bins.append(
                    CalibrationBin(
                        predicted=float(m.group(2)),
                        observed=float(m.group(3)),
                        n=int(m.group(4)),
                    )
                )
            except ValueError:
                continue
        if not bins:
            return None
        return cls.from_bins(bins)

    # ── queries ────────────────────────────────────────────────────────────

    @property
    def best_observed(self) -> float:
        if not self.bins:
            return 1.0
        return max(b.observed for b in self.bins)

    @property
    def base_rate_estimate(self) -> float:
        """Sample-size-weighted observed mean across bins."""
        total = sum(b.n for b in self.bins)
        if total <= 0:
            return 0.5
        return sum(b.observed * b.n for b in self.bins) / total

    @property
    def min_predicted(self) -> float:
        return min(b.predicted for b in self.bins) if self.bins else 0.0

    @property
    def max_predicted(self) -> float:
        return max(b.predicted for b in self.bins) if self.bins else 1.0

    def lowest_threshold_for(self, target_win_rate: float) -> float:
        """Smallest ``predicted`` whose ``observed`` clears ``target_win_rate``.

        If the target is below every bin's observed value, return the
        lowest predicted (most permissive). If above every bin's
        observed value, return 1.0 (never admit) — the caller is
        expected to clamp the *target* first using ``best_observed``.
        """
        if not self.bins:
            return float(target_win_rate)
        ordered = self.bins  # already sorted ascending by predicted
        for b in ordered:
            if b.observed >= target_win_rate:
                return float(b.predicted)
        return 1.0
