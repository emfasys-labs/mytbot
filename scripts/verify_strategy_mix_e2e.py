"""
One-shot verification of strategy_candidate_log aggregation (D033).

Uses in-memory SQLite (no Postgres required). Run:
  python scripts/verify_strategy_mix_e2e.py

Not part of the default test suite; for operator/CI spot-checks.
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timezone
from decimal import Decimal

# Ensure project root is on path when run as script
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

os.environ.setdefault("STRATEGY_CANDIDATE_LOG", "1")


async def _run() -> int:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    from storage.models import StrategyCandidateLog
    from system.strategy_candidate_log import (
        fetch_strategy_mix_diagnostics,
        persist_rows,
        row,
    )

    eng = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with eng.begin() as conn:
        await conn.run_sync(StrategyCandidateLog.__table__.create, checkfirst=True)

    factory = async_sessionmaker(eng, expire_on_commit=False, class_=AsyncSession)
    now = datetime.now(timezone.utc)
    rows = [
        row(
            symbol="SPY", strategy="momentum_breakout", status="no_setup", reason="no_signal",
            loop_iteration=1, metadata={"near_miss_primary": "volume_confirms"},
        ),
        row(
            symbol="TSLA", strategy="momentum_breakout", status="no_setup", reason="no_signal",
            loop_iteration=1, metadata={"near_miss_primary": "volume_confirms"},
        ),
        row(
            symbol="SPY", strategy="mean_reversion", status="no_setup", reason="no_signal",
            loop_iteration=1,
        ),
        row(
            symbol="SPY", strategy="mean_reversion", status="generated", reason="raw_to_signal_candidate",
            side="long", confidence=0.72, adjusted_strength=Decimal("0.70"), loop_iteration=1,
        ),
        row(
            symbol="AAPL", strategy="momentum_breakout", status="generated", reason="raw_to_signal_candidate",
            side="long", confidence=0.55, loop_iteration=1,
        ),
        row(
            symbol="AAPL", strategy="momentum_breakout", status="lost_to_strategy", reason="same_symbol_dedupe",
            side="long", confidence=0.55, winner_strategy="mean_reversion", loop_iteration=1,
            metadata={"loser_score": "0.42", "winner_score": "0.88"},
        ),
        row(
            symbol="QQQ", strategy="event_driven_news", status="skipped", reason="ai_result_unavailable",
            loop_iteration=1, metadata={"near_miss_primary": "ai_result_unavailable"},
        ),
        row(
            symbol="MSFT", strategy="volume_flow", status="filtered_regime", reason="macro_regime_gate",
            loop_iteration=1,
        ),
        row(
            symbol="XLE", strategy="d015_allocator", status="selected_for_allocation", reason="d015_plan_instruction",
            loop_iteration=1,
            metadata={"path": "d015", "action": "open"},
        ),
        row(
            symbol="XLE", strategy="momentum_breakout", status="risk_rejected", reason="max_positions",
            loop_iteration=1, metadata={"path": "d015", "checks_failed": ["max_positions"]},
        ),
        row(
            symbol="XLE", strategy="mean_reversion", status="execution_incomplete", reason="execution_no_result",
            loop_iteration=1, metadata={"execution_stage": "no_order_from_engine"},
        ),
        row(
            symbol="IWM", strategy="mean_reversion", status="executed", reason="order_filled",
            loop_iteration=1, metadata={"path": "legacy"},
        ),
    ]
    n = await persist_rows(factory, rows)
    if n != len(rows):
        print("FAIL: persist_rows count", n, "expected", len(rows))
        return 1

    out = await fetch_strategy_mix_diagnostics(factory, since_hours=24.0)
    print("=== fetch_strategy_mix_diagnostics (24h) ===")
    import json

    print(json.dumps(out, indent=2, default=str))

    # Contract checks
    by_name = {s["name"]: s for s in out.get("strategies", [])}
    assert "mean_reversion" in by_name
    assert by_name["mean_reversion"]["evaluated"] >= 3
    assert by_name["mean_reversion"]["counts"]["generated"] >= 1
    ed = by_name.get("event_driven_news")
    assert ed is not None
    assert ed["counts"].get("skipped", 0) >= 1
    assert (ed.get("top_skip_reason") or {}).get("reason") == "ai_result_unavailable"

    mb = by_name["momentum_breakout"]
    assert mb["counts"]["lost_to_strategy"] >= 1
    assert mb["by_status"].get("lost_to_strategy", 0) >= 1
    tfc = mb.get("top_failed_conditions") or []
    assert any(x.get("key") == "volume_confirms" for x in tfc)
    assert mb.get("blocker_hint")
    mrr = by_name.get("mean_reversion")
    assert mrr
    tei_mr = mrr.get("top_execution_incomplete") or []
    assert any(x.get("reason") == "execution_no_result" for x in tei_mr)
    assert mrr.get("blocker_hint") and "execution" in (mrr.get("blocker_hint") or "").lower()
    # Dedupe reason lives on individual rows; aggregate is by status only.

    print("\n=== contract OK ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_run()))
