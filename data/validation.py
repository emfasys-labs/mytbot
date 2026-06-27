"""
Validate ordered OHLCV bars: ordering, OHLC consistency, gaps, staleness.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import numpy as np
import pandas as pd


@dataclass
class ValidationResult:
    ok: bool
    issues: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)

    def to_json_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "issues": self.issues, **self.meta}


def _to_utc(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def validate_ohlcv_frame(
    df: pd.DataFrame,
    *,
    expected_interval: timedelta | None,
    max_gap_multiplier: float = 5.0,
    stale_after: timedelta | None = None,
    now_utc: datetime | None = None,
) -> ValidationResult:
    """
    ``df`` must have DatetimeIndex or column ``timestamp`` (UTC-aware or naive).
    Columns: open, high, low, close, volume (case-insensitive accepted).
    """
    issues: list[str] = []
    meta: dict[str, Any] = {}

    if df is None or df.empty:
        return ValidationResult(False, ["empty_frame"], meta)

    work = df.copy()
    if isinstance(work.index, pd.DatetimeIndex):
        work = work.reset_index()
        ts_col = work.columns[0]
        work = work.rename(columns={ts_col: "timestamp"})
    elif "timestamp" in work.columns:
        pass
    else:
        return ValidationResult(False, ["missing_timestamp_index_or_column"], meta)

    work["timestamp"] = pd.to_datetime(work["timestamp"], utc=True)
    work = work.sort_values("timestamp")
    ts = work["timestamp"]

    if not ts.is_monotonic_increasing:
        issues.append("timestamps_not_monotonic")
    dup = int(ts.duplicated().sum())
    if dup:
        issues.append(f"duplicate_timestamps:{dup}")

    colmap = {c.lower(): c for c in work.columns}
    for need in ("open", "high", "low", "close", "volume"):
        if need not in colmap:
            return ValidationResult(False, [f"missing_column:{need}"], meta)

    def _dcol(name: str) -> pd.Series:
        return work[colmap[name]].map(lambda x: Decimal(str(x)))

    numeric_ohlcv = work[
        [colmap[name] for name in ("open", "high", "low", "close", "volume")]
    ].apply(pd.to_numeric, errors="coerce")
    non_finite = int(
        (~np.isfinite(numeric_ohlcv.to_numpy(dtype=float))).any(axis=1).sum()
    )
    if non_finite:
        issues.append(f"non_finite_ohlcv_rows:{non_finite}")

    o = _dcol("open")
    h = _dcol("high")
    l_ = _dcol("low")
    c = _dcol("close")

    if (h < l_).any():
        issues.append("high_less_than_low")
    if ((o > h) | (o < l_) | (c > h) | (c < l_)).any():
        issues.append("ohlc_inconsistent_with_hl_range")

    if expected_interval is not None and len(ts) >= 2:
        deltas = ts.diff().dropna()
        if len(deltas):
            median_td = deltas.median()
            meta["median_bar_delta_sec"] = float(median_td.total_seconds())
            threshold = expected_interval * max_gap_multiplier
            big = int((deltas > threshold).sum())
            if big:
                issues.append(f"large_gaps_count:{big}")

    now = _to_utc(now_utc or datetime.now(timezone.utc))
    last = ts.iloc[-1].to_pydatetime()
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    else:
        last = last.astimezone(timezone.utc)
    meta["last_bar_utc"] = last.isoformat()
    if stale_after is not None and (now - last) > stale_after:
        issues.append(
            f"stale_data:last_bar_age_sec:{int((now - last).total_seconds())}"
        )

    return ValidationResult(len(issues) == 0, issues, meta)


def validate_fetched_timestamps(
    timestamps: list[datetime],
    *,
    now_utc: datetime | None = None,
    max_future_skew: timedelta = timedelta(hours=1),
) -> ValidationResult:
    """Reject rows whose timestamps are far in the future (bad clock / API data)."""
    issues: list[str] = []
    now = _to_utc(now_utc or datetime.now(timezone.utc))
    for t in timestamps:
        tu = _to_utc(t)
        if tu > now + max_future_skew:
            issues.append(f"future_timestamp:{tu.isoformat()}")
            break
    return ValidationResult(len(issues) == 0, issues, {})
