"""
Online meta-label calibration hooks.

Computes lightweight per-strategy prior-bias updates from recent execution
outcomes (filled vs rejected/cancelled), then exposes a bounded bias map that
can be merged into `meta_labeling.strategy_bias`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select

from storage.models import OrderLog, SignalLog


@dataclass
class StrategyCalibration:
    strategy: str
    samples: int
    fill_rate: float
    bias_delta: float


def _clip(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, float(x)))


def bias_from_outcomes(
    rows: list[tuple[str, str]],
    *,
    min_samples: int = 20,
    max_abs_delta: float = 0.12,
) -> dict[str, StrategyCalibration]:
    """
    Build per-strategy bias deltas from `(strategy, order_status)` rows.
    """
    by: dict[str, list[str]] = {}
    for strategy, status in rows:
        s = str(strategy or "").strip()
        st = str(status or "").strip().lower()
        if not s or not st:
            continue
        by.setdefault(s, []).append(st)

    out: dict[str, StrategyCalibration] = {}
    for s, stats in by.items():
        n = len(stats)
        if n < min_samples:
            continue
        fills = sum(1 for st in stats if st in {"filled", "partially_filled"})
        fill_rate = fills / n
        # Center at 0.5 and shrink toward zero on low sample counts.
        centered = (fill_rate - 0.5) * 2.0
        shrink = n / (n + 40.0)
        delta = _clip(centered * shrink * max_abs_delta, -max_abs_delta, max_abs_delta)
        out[s] = StrategyCalibration(
            strategy=s,
            samples=n,
            fill_rate=fill_rate,
            bias_delta=delta,
        )
    return out


async def compute_dynamic_strategy_bias(
    session_factory: Any,
    *,
    lookback_hours: int = 48,
    min_samples: int = 20,
    max_abs_delta: float = 0.12,
) -> tuple[dict[str, float], dict[str, Any]]:
    if session_factory is None:
        return {}, {"error": "session_factory_missing"}
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max(1, int(lookback_hours)))
    async with session_factory() as session:
        stmt = (
            select(SignalLog.strategy, OrderLog.status)
            .join(OrderLog, OrderLog.signal_id == SignalLog.id)
            .where(OrderLog.timestamp >= cutoff)
        )
        q = await session.execute(stmt)
        rows = [(str(r[0] or ""), str(r[1] or "")) for r in q.all()]
    cals = bias_from_outcomes(rows, min_samples=min_samples, max_abs_delta=max_abs_delta)
    bias = {k: float(v.bias_delta) for k, v in cals.items()}
    diag = {
        "lookback_hours": int(lookback_hours),
        "rows": len(rows),
        "strategies": {
            k: {"samples": v.samples, "fill_rate": round(v.fill_rate, 4), "bias_delta": round(v.bias_delta, 4)}
            for k, v in cals.items()
        },
    }
    return bias, diag
