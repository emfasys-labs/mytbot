from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from math import isfinite
from typing import Any

from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from intelligence.trade_admission.model import AdmissionModel
from storage.models import FillLog, PriceHistory, TradeAdmissionLog

_STOP_DERISK_SOURCES = {"stop_loss", "intraday_derisk", "aggregate_derisk"}
_FAIL_TOKENS = ("fail", "error", "reject", "blocked")
_ROUTE_TOKENS = ("route", "venue", "closed", "no_broker", "precheck", "unroutable")


def _dec(v: Any) -> Decimal | None:
    if v is None:
        return None
    if isinstance(v, Decimal):
        return v if v.is_finite() else None
    try:
        d = Decimal(str(v))
    except Exception:  # noqa: BLE001
        return None
    return d if d.is_finite() else None


def _json_safe(v: Any) -> Any:
    if v is None or isinstance(v, (str, bool, int)):
        return v
    if isinstance(v, float):
        return v if isfinite(v) else None
    if isinstance(v, Decimal):
        return float(v) if v.is_finite() else None
    if isinstance(v, dict):
        return {str(k): _json_safe(val) for k, val in v.items()}
    if isinstance(v, (list, tuple, set)):
        return [_json_safe(item) for item in v]
    return str(v)


async def insert_admission(
    session_factory: async_sessionmaker[AsyncSession] | None,
    *,
    row_id: str,
    timestamp: datetime,
    loop_iteration: int | None,
    symbol: str,
    strategy: str,
    side: str | None,
    broker: str | None,
    asset_class: str | None,
    signal_id: str | None,
    source_path: str,
    decision: str,
    reason: str,
    shadow_only: bool,
    active_applied: bool,
    admission_score: Decimal | None,
    uncertainty: Decimal | None,
    suggested_notional: Decimal | None,
    suggested_quantity: Decimal | None,
    suggested_price: Decimal | None,
    features: dict[str, Any] | None,
    metadata: dict[str, Any] | None,
) -> None:
    if session_factory is None:
        return
    async with session_factory() as session:
        session.add(
            TradeAdmissionLog(
                id=row_id or str(uuid.uuid4()),
                timestamp=timestamp,
                loop_iteration=loop_iteration,
                symbol=(symbol or "")[:72],
                strategy=(strategy or "unknown")[:64],
                side=(side or None),
                broker=(broker or None),
                asset_class=(asset_class or None),
                signal_id=(signal_id or None),
                source_path=(source_path or "unknown")[:32],
                decision=decision[:32],
                reason=reason[:20000] if reason else None,
                shadow_only=bool(shadow_only),
                active_applied=bool(active_applied),
                admission_score=admission_score,
                uncertainty=uncertainty,
                suggested_notional=suggested_notional,
                suggested_quantity=suggested_quantity,
                suggested_price=suggested_price,
                features=_json_safe(features),
                metadata_=_json_safe(metadata),
            )
        )
        try:
            await session.commit()
        except Exception as exc:  # noqa: BLE001
            await session.rollback()
            logger.debug("trade_admission | insert failed | {}", exc)


async def update_downstream_status(
    session_factory: async_sessionmaker[AsyncSession] | None,
    row_id: str | None,
    *,
    status: str,
    reason: str | None = None,
    execution_status: str | None = None,
) -> None:
    if session_factory is None or not row_id:
        return
    async with session_factory() as session:
        row = await session.get(TradeAdmissionLog, row_id)
        if row is None:
            return
        row.downstream_status = status[:40]
        row.downstream_reason = reason[:20000] if reason else None
        row.execution_status = execution_status[:40] if execution_status else None
        try:
            await session.commit()
        except Exception as exc:  # noqa: BLE001
            await session.rollback()
            logger.debug("trade_admission | status update failed | {}", exc)


async def _price_excursion(
    session: AsyncSession,
    *,
    symbol: str,
    side: str | None,
    entry: Decimal | None,
    start: datetime,
    end: datetime,
) -> dict[str, Any]:
    """Max adverse / favorable move (as fractions of entry) over a window.

    Direction-aware: for a long, favorable = up, adverse = down; mirrored for a
    short. Returns ``{}`` when no entry price or price history is available.
    """
    out: dict[str, Any] = {}
    if entry is None or entry <= 0 or not symbol:
        return out
    try:
        rows = (
            await session.execute(
                select(PriceHistory.high, PriceHistory.low)
                .where(
                    PriceHistory.symbol == symbol,
                    PriceHistory.timestamp > start,
                    PriceHistory.timestamp <= end,
                )
                .order_by(PriceHistory.timestamp.asc())
                .limit(5000)
            )
        ).all()
    except Exception:  # noqa: BLE001
        return out
    highs = [h for h, _ in ((_dec(a), _dec(b)) for a, b in rows) if h is not None]
    lows = [l for _, l in ((_dec(a), _dec(b)) for a, b in rows) if l is not None]
    if not highs or not lows:
        return out
    hi = max(highs)
    lo = min(lows)
    up = (hi - entry) / entry
    down = (entry - lo) / entry
    is_short = str(side or "").lower() in {"sell", "short"}
    favorable = down if is_short else up
    adverse = up if is_short else down
    out["max_favorable_move"] = float(max(Decimal("0"), favorable))
    out["max_adverse_move"] = float(max(Decimal("0"), adverse))
    out["became_profitable_later"] = bool(favorable > 0 and adverse > 0)
    return out


def _status_flags(row: TradeAdmissionLog) -> dict[str, bool]:
    blob = " ".join(
        str(x or "").lower()
        for x in (row.downstream_status, row.downstream_reason, row.execution_status)
    )
    return {
        "execution_failed": any(t in blob for t in _FAIL_TOKENS),
        "venue_closed_or_bad_route": any(t in blob for t in _ROUTE_TOKENS),
    }


def _horizon_snapshot(fills: list[dict[str, Any]], end: datetime) -> dict[str, Any]:
    """Aggregate fill-derived outcome for fills that landed by ``end``."""
    within = [f for f in fills if f["ts"] is not None and f["ts"] <= end]
    pnl = sum((f["pnl"] for f in within), Decimal("0"))
    notional = sum((f["notional"] for f in within), Decimal("0"))
    needed_derisk = any(f["derisk"] for f in within)
    hit_stop = any(f["derisk"] in _STOP_DERISK_SOURCES for f in within)
    # churn: an opening fill that lands after the book had flattened.
    flattened = False
    churn = False
    for f in within:
        if f["qty_after"] == 0:
            flattened = True
        elif flattened:
            churn = True
    snap: dict[str, Any] = {
        "fills": len(within),
        "net_return_after_costs": float(pnl / notional) if notional > 0 else None,
        "net_pnl": float(pnl),
        "needed_derisk": needed_derisk,
        "hit_stop_like_condition": hit_stop,
        "churn_reentry": churn,
    }
    if within:
        snap["better_than_book_holding"] = bool(pnl > 0)
        snap["worse_than_cash"] = bool(pnl < 0)
    return snap


async def label_due_outcomes(
    session_factory: async_sessionmaker[AsyncSession] | None,
    *,
    horizons_minutes: tuple[int, ...] | list[int],
    limit: int = 500,
) -> int:
    """Attach fill- and price-derived outcomes to matured admission rows.

    A row is finalised once its *longest* horizon has elapsed; at that point all
    shorter horizons are computed from stored data in a single pass. Direct
    ``signal_id`` fill matches drive realised P&L; price history drives adverse/
    favorable excursion. Rich myTbot-native labels at the longest horizon are
    written to ``outcome_labels``; per-horizon snapshots to ``outcome_horizons``.
    """
    if session_factory is None:
        return 0
    horizons = sorted({max(1, int(h)) for h in horizons_minutes} or {60})
    max_h = horizons[-1]
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=max_h)
    async with session_factory() as session:
        q = await session.execute(
            select(TradeAdmissionLog)
            .where(
                TradeAdmissionLog.timestamp <= cutoff,
                TradeAdmissionLog.outcome_label.is_(None),
            )
            .order_by(TradeAdmissionLog.timestamp.asc())
            .limit(max(1, limit))
        )
        rows = list(q.scalars().all())
        updated = 0
        for row in rows:
            start = row.timestamp
            if start is not None and start.tzinfo is None:
                start = start.replace(tzinfo=timezone.utc)
            fills: list[dict[str, Any]] = []
            if row.signal_id:
                fq = await session.execute(
                    select(
                        FillLog.timestamp,
                        FillLog.realised_pnl,
                        FillLog.fee,
                        FillLog.notional,
                        FillLog.derisk_source,
                        FillLog.position_qty_after,
                    ).where(FillLog.signal_id == row.signal_id)
                )
                for ts, rpnl, fee, notional, derisk, qty_after in fq.all():
                    if ts is not None and ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                    fills.append(
                        {
                            "ts": ts,
                            "pnl": (_dec(rpnl) or Decimal("0")) - (_dec(fee) or Decimal("0")),
                            "notional": _dec(notional) or Decimal("0"),
                            "derisk": str(derisk or "").strip().lower(),
                            "qty_after": _dec(qty_after) or Decimal("0"),
                        }
                    )

            horizon_snaps: dict[str, Any] = {}
            for h in horizons:
                end = start + timedelta(minutes=h)
                if end > now:
                    continue
                snap = _horizon_snapshot(fills, end)
                snap.update(
                    await _price_excursion(
                        session,
                        symbol=row.symbol,
                        side=row.side,
                        entry=_dec(row.suggested_price),
                        start=start,
                        end=end,
                    )
                )
                horizon_snaps[str(h)] = snap

            # Coarse summary + rich labels from the longest matured horizon.
            longest = horizon_snaps.get(str(max_h)) or (
                horizon_snaps[max(horizon_snaps, key=lambda k: int(k))] if horizon_snaps else {}
            )
            executed = bool(longest.get("fills"))
            net_pnl = Decimal(str(longest.get("net_pnl", 0) or 0))
            status_flags = _status_flags(row)
            rich = dict(longest)
            rich.update(status_flags)
            row.outcome_horizons = _json_safe(horizon_snaps)
            row.outcome_labels = _json_safe(rich)
            row.outcome_observed_at = now
            if not executed:
                row.outcome_label = "not_executed"
                row.outcome_net_pnl = Decimal("0")
                row.outcome_return = None
            else:
                row.outcome_net_pnl = net_pnl
                ret = longest.get("net_return_after_costs")
                row.outcome_return = _dec(ret)
                row.outcome_label = "positive" if net_pnl > 0 else ("negative" if net_pnl < 0 else "flat")
            updated += 1
        if updated:
            try:
                await session.commit()
            except Exception as exc:  # noqa: BLE001
                await session.rollback()
                logger.debug("trade_admission | outcome labeling failed | {}", exc)
                return 0
        return updated


async def train_admission_model(
    session_factory: async_sessionmaker[AsyncSession] | None,
    *,
    lookback_days: int = 30,
    min_samples: int = 25,
) -> AdmissionModel:
    """Build the calibrated model from matured, executed (win/loss) rows."""
    if session_factory is None:
        return AdmissionModel.empty(min_samples)
    since = datetime.now(timezone.utc) - timedelta(days=max(1, int(lookback_days)))
    async with session_factory() as session:
        q = await session.execute(
            select(
                TradeAdmissionLog.strategy,
                TradeAdmissionLog.asset_class,
                TradeAdmissionLog.admission_score,
                TradeAdmissionLog.outcome_label,
            ).where(
                TradeAdmissionLog.timestamp >= since,
                TradeAdmissionLog.outcome_label.in_(("positive", "negative")),
            )
        )
        rows = [
            {
                "strategy": strat,
                "asset_class": ac,
                "score": _dec(score),
                "win": str(label) == "positive",
            }
            for strat, ac, score, label in q.all()
        ]
    return AdmissionModel.from_outcomes(rows, min_samples=min_samples)


async def fetch_admission_diagnostics(
    session_factory: async_sessionmaker[AsyncSession] | None,
    *,
    since_hours: float = 24.0,
    limit: int = 50,
    model_health: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if session_factory is None:
        return {"enabled": False, "reason": "no_session_factory", "rows": []}
    since = datetime.now(timezone.utc) - timedelta(hours=max(1.0, float(since_hours)))
    out: dict[str, Any] = {
        "since_hours": since_hours,
        "aggregate": {},
        "by_strategy": [],
        "rows": [],
        "model_health": model_health or {},
        "estimates": {},
        "coverage_by_asset_class": [],
        "top_rejection_reasons": [],
    }
    _BLOCKERS = {"reject", "defer", "close_only"}
    async with session_factory() as session:
        try:
            aq = await session.execute(
                select(
                    TradeAdmissionLog.decision,
                    TradeAdmissionLog.downstream_status,
                    TradeAdmissionLog.outcome_label,
                    func.count().label("n"),
                )
                .where(TradeAdmissionLog.timestamp >= since)
                .group_by(
                    TradeAdmissionLog.decision,
                    TradeAdmissionLog.downstream_status,
                    TradeAdmissionLog.outcome_label,
                )
            )
            by_decision: dict[str, int] = {}
            by_downstream: dict[str, int] = {}
            by_outcome: dict[str, int] = {}
            total = 0
            for dec, down, outc, n in aq.all():
                c = int(n)
                total += c
                by_decision[str(dec or "unknown")] = by_decision.get(str(dec or "unknown"), 0) + c
                by_downstream[str(down or "pending")] = by_downstream.get(str(down or "pending"), 0) + c
                by_outcome[str(outc or "pending")] = by_outcome.get(str(outc or "pending"), 0) + c
            out["aggregate"] = {
                "total": total,
                "by_decision": dict(sorted(by_decision.items())),
                "by_downstream": dict(sorted(by_downstream.items())),
                "by_outcome": dict(sorted(by_outcome.items())),
            }
            sq = await session.execute(
                select(TradeAdmissionLog.strategy, TradeAdmissionLog.decision, func.count().label("n"))
                .where(TradeAdmissionLog.timestamp >= since)
                .group_by(TradeAdmissionLog.strategy, TradeAdmissionLog.decision)
            )
            by_strategy: dict[str, dict[str, int]] = {}
            for strat, dec, n in sq.all():
                by_strategy.setdefault(str(strat or "unknown"), {})[str(dec or "unknown")] = int(n)
            out["by_strategy"] = [
                {"strategy": k, "by_decision": dict(sorted(v.items())), "total": sum(v.values())}
                for k, v in sorted(by_strategy.items())
            ]
            rq = await session.execute(
                select(TradeAdmissionLog)
                .where(TradeAdmissionLog.timestamp >= since)
                .order_by(TradeAdmissionLog.timestamp.desc())
                .limit(max(1, limit))
            )
            rows = []
            for r in rq.scalars().all():
                rows.append(
                    {
                        "id": r.id,
                        "timestamp": r.timestamp.isoformat() if r.timestamp else None,
                        "symbol": r.symbol,
                        "strategy": r.strategy,
                        "side": r.side,
                        "broker": r.broker,
                        "decision": r.decision,
                        "reason": r.reason,
                        "shadow_only": bool(r.shadow_only),
                        "active_applied": bool(r.active_applied),
                        "score": str(r.admission_score) if r.admission_score is not None else None,
                        "uncertainty": str(r.uncertainty) if r.uncertainty is not None else None,
                        "downstream_status": r.downstream_status,
                        "downstream_reason": r.downstream_reason,
                        "execution_status": r.execution_status,
                        "outcome_label": r.outcome_label,
                        "outcome_net_pnl": str(r.outcome_net_pnl) if r.outcome_net_pnl is not None else None,
                    }
                )
            out["rows"] = rows

            # Operational estimates from matured rows: what the blocking
            # decisions (reject/defer/close_only) avoided or missed.
            eq = await session.execute(
                select(
                    TradeAdmissionLog.decision,
                    TradeAdmissionLog.asset_class,
                    TradeAdmissionLog.outcome_label,
                    TradeAdmissionLog.outcome_labels,
                ).where(TradeAdmissionLog.timestamp >= since)
            )
            avoided_drawdown = 0.0
            missed_winner = 0.0
            cov_total: dict[str, int] = {}
            cov_labelled: dict[str, int] = {}
            for dec, ac, outc, labels in eq.all():
                ac_key = str(ac or "unknown")
                cov_total[ac_key] = cov_total.get(ac_key, 0) + 1
                if outc is not None:
                    cov_labelled[ac_key] = cov_labelled.get(ac_key, 0) + 1
                lab = labels if isinstance(labels, dict) else {}
                adverse = lab.get("max_adverse_move")
                favorable = lab.get("max_favorable_move")
                if str(dec or "").lower() in _BLOCKERS:
                    if isinstance(adverse, (int, float)):
                        avoided_drawdown += float(adverse)
                    if isinstance(favorable, (int, float)):
                        missed_winner += float(favorable)
            out["estimates"] = {
                "avoided_drawdown_move": round(avoided_drawdown, 6),
                "missed_winner_move": round(missed_winner, 6),
            }
            out["coverage_by_asset_class"] = [
                {
                    "asset_class": k,
                    "candidates": cov_total[k],
                    "labelled": cov_labelled.get(k, 0),
                    "coverage": round(cov_labelled.get(k, 0) / cov_total[k], 4) if cov_total[k] else 0.0,
                }
                for k in sorted(cov_total)
            ]

            rr = await session.execute(
                select(TradeAdmissionLog.reason, func.count().label("n"))
                .where(
                    TradeAdmissionLog.timestamp >= since,
                    func.lower(TradeAdmissionLog.decision).in_(tuple(_BLOCKERS)),
                )
                .group_by(TradeAdmissionLog.reason)
                .order_by(func.count().desc())
                .limit(10)
            )
            out["top_rejection_reasons"] = [
                {"reason": str(reason or "unknown"), "count": int(n)} for reason, n in rr.all()
            ]
        except Exception as exc:  # noqa: BLE001
            logger.debug("trade_admission | diagnostics failed | {}", exc)
            out["error"] = str(exc)
    return out

