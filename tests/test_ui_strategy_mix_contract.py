"""
Contract tests for Strategy mix / idle behaviour (redesign dashboard).

The production implementation lives in ``ui/src/app/redesign/mapping.ts``:
``mapStrategies``, ``mergeStrategiesWithSignals``, and ``StrategiesScreen`` in
``screens.tsx`` (``rosterIdle``). These Python tests mirror the minimal rules so
we can confirm *why* a single strategy can show "mix 100%" while the rest are
"idle" — it is usually **data in the current snapshot / signal window**, not a
dead trading loop.
"""

from __future__ import annotations

from typing import Any

import pytest


def _strategy_from_row(o: dict[str, Any]) -> str:
    s = o.get("strategy_name") or o.get("strategy")
    if isinstance(s, str) and s.strip():
        return s
    return "accumulator"


def map_strategies_from_opportunities(opportunities: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Mirror of ``mapStrategies`` in ``mapping.ts`` (weight from opportunity scores)."""
    agg: dict[str, dict[str, float | int]] = {}
    grand_total = 0.0
    for o in opportunities:
        name = _strategy_from_row(o)
        raw = o.get("priority_score")
        if raw is None:
            raw = o.get("opportunity_score")
        try:
            score = float(raw) if raw is not None else 0.0
        except (TypeError, ValueError):
            score = 0.0
        if not (score == score and score >= 0):  # not NaN
            score = 0.0
        conf_raw = o.get("confidence", 0)
        try:
            conf = float(conf_raw) if conf_raw is not None else 0.0
        except (TypeError, ValueError):
            conf = 0.0
        e = agg.setdefault(name, {"total": 0.0, "count": 0, "confSum": 0.0})
        e["total"] = float(e["total"]) + score
        e["count"] = int(e["count"]) + 1
        e["confSum"] = float(e["confSum"]) + (conf if conf == conf else 0.0)
        grand_total += score
    out: dict[str, dict[str, Any]] = {}
    for name, v in agg.items():
        w = (v["total"] / grand_total) if grand_total > 0 else 0.0
        cnt = int(v["count"])
        avg = float(v["confSum"]) / cnt if cnt else 0.0
        out[name] = {"name": name, "weight": w, "trades": cnt, "sharpe": avg, "winRate": avg}
    return out


def roster_idle(idle: bool | None, weight: float, trades: int) -> bool:
    """Mirror of ``StrategiesScreen`` in ``screens.tsx``: ``rosterIdle`` line."""
    return bool(idle) or (weight == 0 and trades == 0)


def test_only_one_strategy_in_opportunities_yields_one_non_idle_card() -> None:
    """If the allocator snapshot only ranks one strategy, only that card gets non-zero weight."""
    opps = [
        {
            "symbol": "SPY",
            "strategy_name": "mean_reversion",
            "priority_score": 0.7,
            "confidence": 0.63,
        }
        for _ in range(11)
    ]
    mapped = map_strategies_from_opportunities(opps)
    assert set(mapped.keys()) == {"mean_reversion"}
    mr = mapped["mean_reversion"]
    assert mr["weight"] == pytest.approx(1.0)
    assert mr["trades"] == 11
    assert roster_idle(None, float(mr["weight"]), int(mr["trades"])) is False


def test_seeded_strategies_with_zero_opps_are_roster_idle() -> None:
    """Backend seeds every ``loaded_strategies`` row with ``idle: true`` when no opps (mapping.ts)."""
    assert roster_idle(True, 0.0, 0) is True
    assert roster_idle(None, 0.0, 0) is True


def test_two_strategies_split_weight() -> None:
    opps = [
        {"strategy_name": "a", "priority_score": 1.0, "confidence": 0.5},
        {"strategy_name": "b", "priority_score": 1.0, "confidence": 0.5},
    ]
    m = map_strategies_from_opportunities(opps)
    assert m["a"]["weight"] == pytest.approx(0.5)
    assert m["b"]["weight"] == pytest.approx(0.5)
    assert roster_idle(None, 0.5, 1) is False
