"""
Persist pre-risk strategy candidate / skip events (D033).

Separate from :class:`storage.models.SignalLog`, which is execution-path only.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from loguru import logger
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from storage.models import StrategyCandidateLog

_LOG_ENABLED = os.getenv("STRATEGY_CANDIDATE_LOG", "1").strip().lower() in ("1", "true", "yes", "on")


def row(
    *,
    symbol: str,
    strategy: str,
    status: str,
    reason: str | None = None,
    side: str | None = None,
    confidence: Decimal | float | None = None,
    adjusted_strength: Decimal | None = None,
    loop_iteration: int | None = None,
    winner_strategy: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a row dict for :func:`persist_rows`."""
    return {
        "id": str(uuid.uuid4()),
        "timestamp": datetime.now(timezone.utc),
        "loop_iteration": loop_iteration,
        "symbol": (symbol or "").strip()[:72],
        "strategy": (strategy or "").strip()[:50],
        "side": (side or None),
        "confidence": confidence,
        "adjusted_strength": adjusted_strength,
        "status": status,
        "reason": reason,
        "winner_strategy": winner_strategy,
        "metadata_": dict(metadata) if metadata else None,
    }


def _d(v: Any) -> Decimal | None:
    if v is None:
        return None
    if isinstance(v, Decimal):
        return v
    try:
        return Decimal(str(v))
    except Exception:  # noqa: BLE001
        return None


async def persist_rows(
    session_factory: async_sessionmaker[AsyncSession],
    rows: list[dict[str, Any]],
) -> int:
    """Insert rows in one transaction. Returns count inserted."""
    if not _LOG_ENABLED or not rows:
        return 0
    cap = 8000
    try:
        cap = max(100, int(os.getenv("STRATEGY_CANDIDATE_LOG_MAX_PER_CYCLE", "8000")))
    except (TypeError, ValueError):
        pass
    rows = rows[:cap]
    async with session_factory() as session:
        for r in rows:
            rec = StrategyCandidateLog(
                id=str(r.get("id") or uuid.uuid4()),
                timestamp=r.get("timestamp") or datetime.now(timezone.utc),
                loop_iteration=r.get("loop_iteration"),
                symbol=str(r.get("symbol", ""))[:72],
                strategy=str(r.get("strategy", ""))[:50],
                side=(str(r["side"])[:8] if r.get("side") else None),
                confidence=_d(r.get("confidence")),
                adjusted_strength=_d(r.get("adjusted_strength")),
                status=str(r.get("status", "unknown"))[:40],
                reason=(str(r["reason"])[:20000] if r.get("reason") else None),
                winner_strategy=(str(r["winner_strategy"])[:50] if r.get("winner_strategy") else None),
                metadata_=r.get("metadata_") or r.get("metadata"),
            )
            session.add(rec)
        try:
            await session.commit()
        except Exception as exc:  # noqa: BLE001
            await session.rollback()
            logger.warning("strategy_candidate_log | batch insert failed: {}", exc)
            return 0
    return len(rows)


_SKIP_STATUSES_FOR_REASON = frozenset(
    {
        "no_setup",
        "skipped",
        "filtered_regime",
        "filtered_signal_engine",
        "filtered_meta",
    }
)


def compute_lifecycle_label(by_status: dict[str, int]) -> str:
    """Derive UI lifecycle key from per-status counts (``strategy_candidate_log``)."""
    bs = by_status
    g = int(bs.get("generated", 0)) + int(bs.get("batched", 0))
    lost = int(bs.get("lost_to_strategy", 0))
    selected = int(bs.get("selected_for_allocation", 0))
    risk = int(bs.get("risk_rejected", 0))
    ex = int(bs.get("executed", 0))
    ev = sum(int(v) for v in bs.values())
    if ev == 0:
        return "idle"
    if ex > 0:
        return "trading"
    if risk > 0 and risk >= max(selected, 1) and risk >= g:
        return "blocked_by_risk"
    if selected > 0:
        return "selected"
    if lost > 0:
        return "competing"
    if g > 0:
        return "finding_setups"
    return "scanning"


# Human labels for :func:`_near_miss_key_label` and Strategy Mix "blocker" line.
NEAR_MISS_LABELS: dict[str, str] = {
    "price_breakout": "Close did not break above rolling high × (1 + momentum threshold)",
    "volume_confirms": "Volume did not confirm (below avg × multiplier)",
    "volatility": "ATR% outside the configured min/max band",
    "atr_below_min": "ATR% below minimum (market too quiet)",
    "atr_above_max": "ATR% above maximum (too volatile)",
    "low_volume_z": "Volume z-score below open threshold",
    "bar_return_too_small": "Bar return too small for flow continuation",
    "trend_or_continuation": "Flow z OK but EMA/continuation rules not met",
    "exhaustion_not_faded": "High volume z but bar not faded for exhaustion",
    "insufficient_rows": "Not enough feature bars in window",
    "in_mid_band": "ATR fast/slow in the middle: no clear expansion/compression",
    "bar_impulse_too_weak_for_expansion": "ATR ratio suggests expansion but bar return too small",
    "bar_too_active_for_compression": "ATR ratio suggests compression but bar return still large",
    "no_clear_regime": "ATR/impulse state did not match a vol-regime leg",
    "not_triggered": "Flow rule set did not fire",
    "no_data": "Missing or empty feature frame",
    "missing_ohlcv": "Missing required OHLCV columns",
    "ai_result_unavailable": "Event-driven: AI / pipeline result unavailable (skipped)",
    "no_symbol_news_context": "No per-symbol news score in AI output",
    "below_shock_threshold": "News |score| below shock threshold",
    "strategy_disabled": "Strategy instance disabled in config",
    "diagnostic_error": "Near-miss snapshot raised an internal error",
    "no_news_context_for_symbol": "No per-symbol news context in AI result",
    "regime_rotation_not_triggered": "Regime rotation demand gate not met",
    "event_not_triggered": "Event shock or context did not pass threshold",
    "no_signal": "No raw signal (generic)",
}


def _near_miss_key_label(key: str | None) -> str:
    if not key:
        return "—"
    return NEAR_MISS_LABELS.get(key, str(key).replace("_", " "))


def _format_top_failed(near_d: dict[str, int] | None) -> list[dict[str, Any]]:
    if not near_d:
        return []
    return [
        {"key": k, "count": int(c), "label": _near_miss_key_label(k)}
        for k, c in sorted(near_d.items(), key=lambda x: -x[1])[:5]
    ]


def _format_top_reasons(ct: dict[str, int] | None) -> list[dict[str, Any]]:
    if not ct:
        return []
    return [
        {"reason": k, "count": int(c)}
        for k, c in sorted(ct.items(), key=lambda x: -x[1])[:5]
    ]


def _pack_strategy_row(
    name: str,
    by_status: dict[str, int],
    last_any: Any,
    last_gen: Any,
    top_reason: tuple[str | None, int] | None,
    top_failed: list[dict[str, Any]] | None = None,
    top_risk: list[dict[str, Any]] | None = None,
    top_exec: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    g = by_status.get("generated", 0) + by_status.get("batched", 0)
    lost = by_status.get("lost_to_strategy", 0)
    selected = by_status.get("selected_for_allocation", 0)
    risk = by_status.get("risk_rejected", 0)
    ex = by_status.get("executed", 0)
    ev = sum(by_status.values())
    tr, tr_c = (top_reason if top_reason else (None, 0))
    tfc = list(top_failed or [])
    trr = list(top_risk or [])
    tei = list(top_exec or [])
    bh = _blocker_hint(
        {**by_status},
        tfc,
        trr,
        tei,
    )
    return {
        "name": name,
        "evaluated": ev,
        "by_status": dict(sorted(by_status.items())),
        "counts": {
            "no_setup": by_status.get("no_setup", 0),
            "generated": g,
            "filtered_regime": by_status.get("filtered_regime", 0),
            "filtered_signal_engine": by_status.get("filtered_signal_engine", 0),
            "filtered_meta": by_status.get("filtered_meta", 0),
            "lost_to_strategy": lost,
            "selected_for_allocation": selected,
            "risk_rejected": risk,
            "executed": ex,
            "skipped": by_status.get("skipped", 0),
            "execution_incomplete": by_status.get("execution_incomplete", 0),
        },
        "last_evaluated_at": last_any.isoformat() if last_any else None,
        "last_generated_at": last_gen.isoformat() if last_gen else None,
        "top_skip_reason": ({"reason": tr, "count": tr_c} if tr else None),
        "top_failed_conditions": tfc,
        "top_risk_rejection_reasons": trr,
        "top_execution_incomplete": tei,
        "blocker_hint": bh,
        "lifecycle": compute_lifecycle_label(by_status),
    }


def _blocker_hint(
    by_status: dict[str, int],
    top_failed: list[dict[str, Any]],
    top_risk: list[dict[str, Any]],
    top_exec: list[dict[str, Any]],
) -> str | None:
    """One-line operator summary: prefer risk, then execution, then near-miss."""
    rj = int(by_status.get("risk_rejected", 0) or 0)
    exn = int(by_status.get("execution_incomplete", 0) or 0)
    if rj > 0 and top_risk:
        r0 = top_risk[0]
        rs = str(r0.get("reason", ""))[:200]
        return f"Risk: {rs} ({int(r0.get('count', 0))}×)"
    if exn > 0 and top_exec:
        e0 = top_exec[0]
        es = str(e0.get("reason", ""))[:200]
        return f"Execution: {es} ({int(e0.get('count', 0))}×)"
    if top_failed:
        f0 = top_failed[0]
        lb = str(f0.get("label", f0.get("key", "")))[:180]
        return f"No setup: {lb} ({int(f0.get('count', 0))}×)"
    return None


async def aggregate_by_strategy(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    since_hours: float = 24.0,
) -> list[dict[str, Any]]:
    """Roll-up counts for /diagnostics — cheap GROUP BY on recent rows (legacy shape)."""
    r = await fetch_strategy_mix_diagnostics(session_factory, since_hours=since_hours)
    return r.get("strategies", [])


async def fetch_strategy_mix_diagnostics(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    since_hours: float = 24.0,
) -> dict[str, Any]:
    """Strategy Mix UI + API: per-strategy counts, timestamps, lifecycle, top skip reason."""
    from datetime import timedelta

    since = datetime.now(timezone.utc) - timedelta(hours=since_hours)
    out: dict[str, Any] = {
        "since_hours": since_hours,
        "strategies": [],
    }
    async with session_factory() as session:
        try:
            r2 = await session.execute(
                select(
                    StrategyCandidateLog.strategy,
                    StrategyCandidateLog.status,
                    func.count().label("c"),
                )
                .where(StrategyCandidateLog.timestamp >= since)
                .group_by(StrategyCandidateLog.strategy, StrategyCandidateLog.status)
            )
            by_strat: dict[str, dict[str, int]] = {}
            for st, stt, c in r2.all():
                by_strat.setdefault(str(st), {})[str(stt)] = int(c)

            rmax = await session.execute(
                select(
                    StrategyCandidateLog.strategy,
                    func.max(StrategyCandidateLog.timestamp),
                )
                .where(StrategyCandidateLog.timestamp >= since)
                .group_by(StrategyCandidateLog.strategy)
            )
            last_any: dict[str, Any] = {str(row[0]): row[1] for row in rmax.all()}

            rgen = await session.execute(
                select(
                    StrategyCandidateLog.strategy,
                    func.max(StrategyCandidateLog.timestamp),
                )
                .where(
                    StrategyCandidateLog.timestamp >= since,
                    StrategyCandidateLog.status.in_(["generated", "batched"]),
                )
                .group_by(StrategyCandidateLog.strategy)
            )
            last_gen: dict[str, Any] = {str(row[0]): row[1] for row in rgen.all()}

            rreason = await session.execute(
                select(
                    StrategyCandidateLog.strategy,
                    StrategyCandidateLog.reason,
                    func.count().label("n"),
                )
                .where(
                    StrategyCandidateLog.timestamp >= since,
                    StrategyCandidateLog.status.in_(_SKIP_STATUSES_FOR_REASON),
                    and_(
                        StrategyCandidateLog.reason.isnot(None),
                        StrategyCandidateLog.reason != "",
                    ),
                )
                .group_by(StrategyCandidateLog.strategy, StrategyCandidateLog.reason)
            )
            reason_rows = list(rreason.all())

            rmeta = await session.execute(
                select(
                    StrategyCandidateLog.strategy,
                    StrategyCandidateLog.status,
                    StrategyCandidateLog.reason,
                    StrategyCandidateLog.metadata_,
                )
                .where(
                    and_(
                        StrategyCandidateLog.timestamp >= since,
                        StrategyCandidateLog.status.in_(
                            [
                                "no_setup",
                                "skipped",
                                "risk_rejected",
                                "execution_incomplete",
                            ]
                        ),
                    )
                )
                .limit(20_000)
            )
            meta_rows = list(rmeta.all())
        except Exception as exc:  # noqa: BLE001
            logger.debug("strategy_candidate_log | fetch_strategy_mix_diagnostics failed: {}", exc)
            return out

    # Pick top (reason, count) per strategy
    top_r: dict[str, tuple[str | None, int]] = {}
    for st, reason, n in reason_rows:
        s = str(st)
        rsn = (str(reason).strip() if reason is not None else None) or None
        prev = top_r.get(s)
        if prev is None or int(n) > prev[1]:
            top_r[s] = (rsn, int(n))

    near_by: dict[str, dict[str, int]] = {}
    rsk_by: dict[str, dict[str, int]] = {}
    exc_by: dict[str, dict[str, int]] = {}
    for st, stt, rsn, meta in meta_rows:
        s = str(st)
        stt = str(stt)
        if stt in ("no_setup", "skipped") and isinstance(meta, dict):
            k = meta.get("near_miss_primary")
            if k:
                near_by.setdefault(s, {})
                ks = str(k)
                near_by[s][ks] = near_by[s].get(ks, 0) + 1
        elif stt == "risk_rejected" and rsn:
            rsk_by.setdefault(s, {})
            rk = str(rsn)[:2000]
            rsk_by[s][rk] = rsk_by[s].get(rk, 0) + 1
        elif stt == "execution_incomplete" and rsn:
            exc_by.setdefault(s, {})
            ek = str(rsn)[:2000]
            exc_by[s][ek] = exc_by[s].get(ek, 0) + 1

    all_names = sorted(set(by_strat) | set(last_any.keys()))
    strategies: list[dict[str, Any]] = []
    for name in all_names:
        bs = dict(by_strat.get(name, {}))
        tr = top_r.get(name)
        strategies.append(
            _pack_strategy_row(
                name,
                bs,
                last_any.get(name),
                last_gen.get(name),
                tr,
                _format_top_failed(near_by.get(name)),
                _format_top_reasons(rsk_by.get(name)),
                _format_top_reasons(exc_by.get(name)),
            )
        )
    out["strategies"] = strategies
    return out
