"""FastAPI backend for M7 control plane + dashboard."""

from __future__ import annotations

import asyncio
import json
import os
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

# Same as runners: uvicorn does not load .env automatically — without this,
# POSTGRES_* and API_CONTROL_TOKEN are missing when starting `uvicorn api.server:app`.
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from fastapi import Depends, FastAPI, Header, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import func, select
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from api.dashboard_layer import gather_ws_events, log_cors_live_warning, merge_risk_parameters_for_api, verify_dashboard_token
from control.command_bus import CommandBus
from control.runtime import get_execution_engine, get_risk_engine
from control.startup_validation import validate_startup_env
from risk.parameters import ParameterManager
from storage.db import dispose_engine, init_async_database
from storage.models import AIOutputLog, AnomalyLog, DailyPnL, FeatureSnapshot, NewsHeadline, OrderLog, PositionLog, RiskLog, SignalLog, ThesisLog

APP_ENV = os.getenv("APP_ENV", "paper")
MUTATION_TOKEN = os.getenv("API_CONTROL_TOKEN", "").strip()
ALLOWED_ORIGINS = [x.strip() for x in os.getenv("API_ALLOWED_ORIGINS", "*").split(",") if x.strip()]

_EXEMPT_READ_AUTH = frozenset(
    {
        "/healthz",
        "/openapi.json",
        "/docs",
        "/redoc",
        "/favicon.ico",
    }
)


class _DashboardReadMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):  # noqa: ANN001
        if request.scope.get("type") == "websocket":
            return await call_next(request)
        if request.method == "OPTIONS":
            return await call_next(request)
        path = request.url.path
        if path in _EXEMPT_READ_AUTH or path.startswith("/docs") or path.startswith("/redoc"):
            return await call_next(request)
        if path == "/auth/dashboard/login" and request.method == "POST":
            return await call_next(request)
        if not os.getenv("DASHBOARD_READ_TOKEN", "").strip():
            return await call_next(request)
        hdr = request.headers.get("x-dashboard-token")
        auth = request.headers.get("authorization")
        if not verify_dashboard_token(hdr, auth):
            return JSONResponse({"detail": "dashboard read unauthorized"}, status_code=401)
        return await call_next(request)


app = FastAPI(title="mytbot Control API", description="Autonomous trading system dashboard API", version="0.3.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS or ["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(_DashboardReadMiddleware)


@app.on_event("startup")
async def _startup() -> None:
    log_cors_live_warning()
    # Gracefully try DB — orchestrator may not have started infra yet when
    # the API is loaded standalone (e.g. uvicorn api.server:app).
    try:
        validate_startup_env(component="api.server", require_postgres=True, strict=False)
    except Exception:
        pass
    engine, session_factory = await init_async_database()
    app.state.db_engine = engine
    app.state.db_session_factory = session_factory
    app.state.command_bus = CommandBus(session_factory) if session_factory is not None else None


@app.on_event("shutdown")
async def _shutdown() -> None:
    await dispose_engine(getattr(app.state, "db_engine", None))


def _require_mutation_token(x_control_token: str | None = Header(default=None, alias="X-Control-Token")) -> None:
    if not MUTATION_TOKEN:
        return
    if x_control_token != MUTATION_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid control token")


def _session_factory():
    sf = getattr(app.state, "db_session_factory", None)
    if sf is None:
        raise HTTPException(status_code=503, detail="Database unavailable")
    return sf


def _command_bus() -> CommandBus:
    bus = getattr(app.state, "command_bus", None)
    if bus is None:
        raise HTTPException(status_code=503, detail="Command bus unavailable")
    return bus


def _decimal_str(v: Any) -> str:
    if v is None:
        return "0"
    return str(Decimal(str(v)))


async def _latest_feature_prices(session, symbols: list[str]) -> dict[str, Decimal]:
    """Best-effort latest close per symbol from feature store."""
    out: dict[str, Decimal] = {}
    for sym in symbols:
        s = (sym or "").strip()
        if not s:
            continue
        try:
            q = await session.execute(
                select(FeatureSnapshot.close)
                .where(FeatureSnapshot.symbol == s)
                .order_by(FeatureSnapshot.bar_timestamp.desc())
                .limit(1)
            )
            close_v = q.scalar_one_or_none()
            if close_v is None:
                continue
            px = Decimal(str(close_v))
            if px > 0:
                out[s] = px
        except Exception:  # noqa: BLE001
            continue
    return out


async def _compute_live_unrealised_mtm(session_factory) -> Decimal:
    """Mark latest position snapshot to latest feature prices."""
    async with session_factory() as session:
        latest_ts_q = await session.execute(select(func.max(PositionLog.timestamp)))
        latest_ts = latest_ts_q.scalar_one_or_none()
        if latest_ts is None:
            return Decimal(0)
        q = await session.execute(select(PositionLog).where(PositionLog.timestamp == latest_ts))
        rows = list(q.scalars().all())
        if not rows:
            return Decimal(0)
        prices = await _latest_feature_prices(session, [r.symbol for r in rows])
        total = Decimal(0)
        for r in rows:
            qty = Decimal(str(r.quantity or 0))
            avg = Decimal(str(r.avg_entry_price or 0))
            if avg <= 0 or qty == 0:
                continue
            current = prices.get(r.symbol, Decimal(str(r.current_price or avg)))
            total += (current - avg) * qty
        return total


def _get_orchestrator():
    """Lazy-import to avoid circular deps when API is loaded standalone."""
    try:
        from system.orchestrator import Orchestrator
        return Orchestrator.get_instance()
    except Exception:
        return None


@app.get("/healthz")
async def healthz():
    return {"ok": True, "service": "api", "ts": datetime.now(timezone.utc).isoformat()}


@app.get("/readyz")
async def readyz():
    sf = getattr(app.state, "db_session_factory", None)
    return {"ok": sf is not None, "db": sf is not None}


@app.get("/status")
async def get_status():
    risk_engine = get_risk_engine()
    execution_engine = get_execution_engine()
    exec_connected = list(getattr(execution_engine, "_brokers", {}).keys()) if execution_engine is not None else []
    bus = getattr(app.state, "command_bus", None)
    runtime = {}
    strategies: dict[str, bool] = {}
    if bus is not None:
        runtime = await bus.get_state("runtime.heartbeat", {}) or {}
        state_map = await bus.get_state_prefix("strategy.enabled.")
        strategies = {k.replace("strategy.enabled.", "", 1): bool(v) for k, v in state_map.items()}

    orch = _get_orchestrator()
    system_state = orch.state.value if orch else "off"
    orch_brokers = orch.status().get("active_brokers", []) if orch else []
    connected_brokers = orch_brokers if orch_brokers else exec_connected

    return {
        "status": "running",
        "system_state": system_state,
        "mode": APP_ENV,
        "paper_mode": APP_ENV != "live",
        "kill_switch": bool(getattr(risk_engine, "is_killed", False)),
        "connected_brokers": connected_brokers or orch_brokers,
        "active_strategies": strategies,
        "runtime": runtime,
    }


@app.get("/positions")
async def get_positions(limit: int = Query(50, ge=1, le=500), session_factory=Depends(_session_factory)):
    async with session_factory() as session:
        latest_ts_q = await session.execute(select(func.max(PositionLog.timestamp)))
        latest_ts = latest_ts_q.scalar_one_or_none()
        if latest_ts is None:
            return {"positions": []}
        q = await session.execute(
            select(PositionLog)
            .where(PositionLog.timestamp == latest_ts)
            .order_by(PositionLog.symbol.asc())
            .limit(limit)
        )
        rows = list(q.scalars().all())
        prices = await _latest_feature_prices(session, [r.symbol for r in rows])
    return {
        "positions": [
            (lambda qty, avg, current: {
                "timestamp": r.timestamp.isoformat() if r.timestamp else None,
                "symbol": r.symbol,
                "broker": r.broker,
                "quantity": _decimal_str(qty),
                "avg_entry_price": _decimal_str(avg),
                "current_price": _decimal_str(current),
                "unrealised_pnl": _decimal_str((current - avg) * qty),
                "asset_class": r.asset_class,
            })(
                Decimal(str(r.quantity or 0)),
                Decimal(str(r.avg_entry_price or 0)),
                prices.get(r.symbol, Decimal(str(r.current_price or 0))),
            )
            for r in rows
        ]
    }


@app.get("/orders")
async def get_orders(limit: int = Query(50, ge=1, le=500), session_factory=Depends(_session_factory)):
    async with session_factory() as session:
        q = await session.execute(select(OrderLog).order_by(OrderLog.timestamp.desc()).limit(limit))
        rows = list(q.scalars().all())
    return {
        "orders": [
            {
                "id": r.id,
                "signal_id": r.signal_id,
                "broker_order_id": r.broker_order_id,
                "timestamp": r.timestamp.isoformat() if r.timestamp else None,
                "symbol": r.symbol,
                "side": r.side,
                "order_type": r.order_type,
                "quantity": _decimal_str(r.quantity),
                "limit_price": _decimal_str(r.limit_price) if r.limit_price is not None else None,
                "broker": r.broker,
                "status": r.status,
                "filled_quantity": _decimal_str(r.filled_quantity) if r.filled_quantity is not None else None,
                "avg_fill_price": _decimal_str(r.avg_fill_price) if r.avg_fill_price is not None else None,
                "fee": _decimal_str(r.fee) if r.fee is not None else None,
                "paper_mode": bool(r.paper_mode),
            }
            for r in rows
        ]
    }


@app.get("/signals")
async def get_signals(limit: int = Query(50, ge=1, le=500), session_factory=Depends(_session_factory)):
    async with session_factory() as session:
        q = await session.execute(select(SignalLog).order_by(SignalLog.timestamp.desc()).limit(limit))
        rows = list(q.scalars().all())
    return {
        "signals": [
            {
                "id": r.id,
                "timestamp": r.timestamp.isoformat() if r.timestamp else None,
                "symbol": r.symbol,
                "side": r.side,
                "strategy": r.strategy,
                "confidence": _decimal_str(r.confidence),
                "asset_class": r.asset_class,
                "broker": r.broker,
                "news_score": _decimal_str(r.news_score) if r.news_score is not None else None,
                "news_veto": bool(r.news_veto),
                "metadata": r.metadata_ or {},
            }
            for r in rows
        ]
    }


@app.get("/news")
async def get_news(limit: int = Query(30, ge=1, le=100), session_factory=Depends(_session_factory)):
    async with session_factory() as session:
        hq = await session.execute(
            select(NewsHeadline)
            .order_by(NewsHeadline.published_at.desc())
            .limit(limit)
        )
        headlines = list(hq.scalars().all())

        aq = await session.execute(
            select(AIOutputLog)
            .where(AIOutputLog.context_type == "news")
            .order_by(AIOutputLog.timestamp.desc())
            .limit(limit)
        )
        ai_rows = list(aq.scalars().all())

    ai_by_symbol: dict[str, dict[str, Any]] = {}
    for r in ai_rows:
        if r.symbol and r.symbol not in ai_by_symbol:
            ai_by_symbol[r.symbol] = {
                "symbol": r.symbol,
                "score": _decimal_str(r.score) if r.score is not None else None,
                "confidence": _decimal_str(r.confidence) if r.confidence is not None else None,
                "event_type": r.event_type,
                "rationale": r.rationale,
                "scored_at": r.timestamp.isoformat() if r.timestamp else None,
            }

    return {
        "headlines": [
            {
                "title": h.title,
                "source": h.source_name,
                "published_at": h.published_at.isoformat() if h.published_at else None,
                "url": h.url,
                "description": h.description,
            }
            for h in headlines
        ],
        "ai_scores": list(ai_by_symbol.values()),
    }


@app.get("/discovery/summary")
async def get_discovery_summary(session_factory=Depends(_session_factory)):
    """Universe funnel + recent anomaly/thesis counts for the Discovery panel."""
    from datetime import timedelta
    from data.universe_tiers import load_universe_tiers

    tiers = load_universe_tiers()
    n_core = len(tiers.core) if tiers else 0
    n_scan = len(tiers.scan) if tiers else 0
    n_light = len(tiers.light) if tiers else 0
    tiers_updated = tiers.updated_at if tiers else None

    orch = _get_orchestrator()
    bm = getattr(orch, "_broker_manager", None) if orch else None
    broker_total: dict[str, int] = {}
    if bm is not None:
        for name, adapter in bm.adapters.items():
            try:
                syms = await asyncio.wait_for(adapter.get_supported_symbols(), timeout=5)
                broker_total[name] = len(syms or [])
            except Exception:  # noqa: BLE001
                broker_total[name] = 0

    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    async with session_factory() as session:
        n_anomalies_q = await session.execute(
            select(func.count()).select_from(AnomalyLog).where(AnomalyLog.timestamp >= cutoff)
        )
        n_anomalies = int(n_anomalies_q.scalar_one() or 0)

        n_theses_q = await session.execute(
            select(func.count()).select_from(ThesisLog).where(ThesisLog.timestamp >= cutoff)
        )
        n_theses = int(n_theses_q.scalar_one() or 0)

        n_signals_q = await session.execute(
            select(func.count()).select_from(SignalLog).where(SignalLog.timestamp >= cutoff)
        )
        n_signals = int(n_signals_q.scalar_one() or 0)

        n_monitored_q = await session.execute(
            select(func.count(SignalLog.symbol.distinct())).where(SignalLog.timestamp >= cutoff)
        )
        n_monitored = int(n_monitored_q.scalar_one() or 0)

    return {
        "universe": {
            "broker_totals": broker_total,
            "total_broker_instruments": sum(broker_total.values()),
            "core": n_core,
            "scan": n_scan,
            "light": n_light,
            "total_tiered": n_core + n_scan + n_light,
            "tiers_updated_at": tiers_updated,
        },
        "last_24h": {
            "anomalies_detected": n_anomalies,
            "theses_generated": n_theses,
            "signals_produced": n_signals,
            "symbols_analysed": n_monitored,
        },
    }


@app.get("/intelligence/regime")
async def get_intelligence_regime(session_factory=Depends(_session_factory)):
    """Latest macro regime + top news-scored symbols."""
    from datetime import timedelta

    cutoff = datetime.now(timezone.utc) - timedelta(hours=48)
    async with session_factory() as session:
        macro_q = await session.execute(
            select(AIOutputLog)
            .where(AIOutputLog.context_type == "macro")
            .order_by(AIOutputLog.timestamp.desc())
            .limit(1)
        )
        macro_row = macro_q.scalars().first()

        news_q = await session.execute(
            select(AIOutputLog)
            .where(AIOutputLog.context_type == "news", AIOutputLog.timestamp >= cutoff, AIOutputLog.symbol.isnot(None))
            .order_by(AIOutputLog.timestamp.desc())
            .limit(200)
        )
        news_rows = list(news_q.scalars().all())

    by_symbol: dict[str, dict] = {}
    for r in news_rows:
        s = (r.symbol or "").strip().upper()
        if not s or s in by_symbol:
            continue
        by_symbol[s] = {
            "symbol": s,
            "score": float(r.score) if r.score is not None else 0.0,
            "event_type": r.event_type or "other",
            "rationale": (r.rationale or "")[:200],
            "scored_at": r.timestamp.isoformat() if r.timestamp else None,
        }

    # Only show symbols with a real score; 0.0 means no relevant news was found.
    top_movers = sorted(
        [v for v in by_symbol.values() if v["score"] != 0.0],
        key=lambda x: abs(x["score"]),
        reverse=True,
    )[:8]

    return {
        "regime": {
            "label": macro_row.regime_label if macro_row else "unknown",
            "confidence": float(macro_row.confidence) if macro_row and macro_row.confidence else 0.0,
            "rationale": (macro_row.rationale or "")[:300] if macro_row else "",
            "updated_at": macro_row.timestamp.isoformat() if macro_row and macro_row.timestamp else None,
        },
        "top_movers": top_movers,
    }


@app.get("/intelligence/signals")
async def get_intelligence_signals(
    limit: int = Query(10, ge=1, le=50),
    session_factory=Depends(_session_factory),
):
    """Recent signals annotated with their risk verdict.
    Prefers signals from the last 2 hours; falls back to latest N if none exist recently.
    """
    from datetime import timedelta
    recent_cutoff = datetime.now(timezone.utc) - timedelta(hours=6)
    async with session_factory() as session:
        # Fetch recent signals; exclude legacy "max_trades_per_day" rejections
        sig_q = await session.execute(
            select(SignalLog)
            .where(SignalLog.timestamp >= recent_cutoff)
            .order_by(SignalLog.timestamp.desc())
            .limit(limit * 3)  # over-fetch so we can filter below
        )
        sigs_raw = list(sig_q.scalars().all())

        # Collect risk verdicts so we can filter out legacy-only rejections
        legacy_signal_ids: set = set()
        if sigs_raw:
            raw_ids = [s.id for s in sigs_raw]
            rl_q = await session.execute(
                select(RiskLog).where(RiskLog.signal_id.in_(raw_ids))
            )
            legacy_check = "max_trades_per_day"
            for rv in rl_q.scalars().all():
                checks = rv.checks_failed or []
                if checks and all(c == legacy_check for c in checks):
                    legacy_signal_ids.add(rv.signal_id)

        sigs = [s for s in sigs_raw if s.id not in legacy_signal_ids][:limit]

        # Absolute fallback: newest N signals from all time (still excluding legacy)
        if not sigs:
            sig_q = await session.execute(
                select(SignalLog).order_by(SignalLog.timestamp.desc()).limit(limit * 3)
            )
            sigs_raw = list(sig_q.scalars().all())
            if sigs_raw:
                raw_ids = [s.id for s in sigs_raw]
                rl_q = await session.execute(
                    select(RiskLog).where(RiskLog.signal_id.in_(raw_ids))
                )
                for rv in rl_q.scalars().all():
                    checks = rv.checks_failed or []
                    if checks and all(c == legacy_check for c in checks):
                        legacy_signal_ids.add(rv.signal_id)
            sigs = [s for s in sigs_raw if s.id not in legacy_signal_ids][:limit]

        verdicts: dict[str, dict] = {}
        if sigs:
            sig_ids = [s.id for s in sigs]
            risk_q = await session.execute(
                select(RiskLog).where(RiskLog.signal_id.in_(sig_ids))
            )
            for rv in risk_q.scalars().all():
                if rv.signal_id not in verdicts:
                    verdicts[rv.signal_id] = {
                        "verdict": rv.verdict,
                        "reason": rv.reason,
                        "checks_failed": rv.checks_failed or [],
                    }


    return {
        "signals": [
            {
                "id": s.id,
                "timestamp": s.timestamp.isoformat() if s.timestamp else None,
                "symbol": s.symbol,
                "side": s.side,
                "strategy": s.strategy,
                "confidence": float(s.confidence) if s.confidence else 0.0,
                "asset_class": s.asset_class,
                "news_score": float(s.news_score) if s.news_score is not None else None,
                "quality_score": (float(s.metadata_["trade_quality_score"]) if s.metadata_ and "trade_quality_score" in s.metadata_ else None),
                "volume_z": (float(s.metadata_["volume_z_score"]) if s.metadata_ and "volume_z_score" in s.metadata_ else None),
                "verdict": verdicts.get(s.id, {}).get("verdict", "unknown"),
                "risk_reason": verdicts.get(s.id, {}).get("reason", ""),
                "checks_failed": verdicts.get(s.id, {}).get("checks_failed", []),
            }
            for s in sigs
        ]
    }


@app.get("/discovery/anomalies")
async def get_discovery_anomalies(limit: int = Query(50, ge=1, le=500), session_factory=Depends(_session_factory)):
    async with session_factory() as session:
        q = await session.execute(select(AnomalyLog).order_by(AnomalyLog.timestamp.desc()).limit(limit))
        rows = list(q.scalars().all())
    return {
        "anomalies": [
            {
                "id": r.id,
                "timestamp": r.timestamp.isoformat() if r.timestamp else None,
                "symbol": r.symbol,
                "asset_class": r.asset_class,
                "direction": r.direction,
                "price_move_pct": _decimal_str(r.price_move_pct),
                "price_z_score": _decimal_str(r.price_z_score),
                "volume_z_score": _decimal_str(r.volume_z_score) if r.volume_z_score is not None else None,
                "news_velocity": _decimal_str(r.news_velocity) if r.news_velocity is not None else None,
                "news_sentiment": _decimal_str(r.news_sentiment) if r.news_sentiment is not None else None,
                "anomaly_score": _decimal_str(r.anomaly_score),
                "opportunities_found": r.opportunities_found,
                "thesis_generated": bool(r.thesis_generated),
                "signals_produced": r.signals_produced,
            }
            for r in rows
        ]
    }


@app.get("/discovery/theses")
async def get_discovery_theses(limit: int = Query(50, ge=1, le=500), session_factory=Depends(_session_factory)):
    async with session_factory() as session:
        q = await session.execute(select(ThesisLog).order_by(ThesisLog.timestamp.desc()).limit(limit))
        rows = list(q.scalars().all())
    return {
        "theses": [
            {
                "id": r.id,
                "timestamp": r.timestamp.isoformat() if r.timestamp else None,
                "trigger_symbol": r.trigger_symbol,
                "trigger_direction": r.trigger_direction,
                "trigger_explanation": r.trigger_explanation,
                "overall_confidence": _decimal_str(r.overall_confidence),
                "time_horizon_hours": r.time_horizon_hours,
                "opportunities": r.opportunities or [],
                "invalidation_conditions": r.invalidation_conditions or [],
                "model_used": r.model_used,
                "tokens_used": r.tokens_used,
                "ai_cost_usd": _decimal_str(r.ai_cost_usd) if r.ai_cost_usd is not None else None,
            }
            for r in rows
        ]
    }


async def _live_portfolio_value() -> Decimal:
    """Sum net-liquidation across all connected brokers."""
    orch = _get_orchestrator()
    if orch is None:
        return Decimal(0)
    bm = getattr(orch, "_broker_manager", None)
    if bm is None:
        return Decimal(0)
    total = Decimal(0)
    for _name, adapter in list(bm.adapters.items()):
        try:
            balances = await adapter.get_balance()
            if not balances:
                continue
            best = max(balances, key=lambda b: b.total)
            if best.total > 0:
                total += best.total
        except Exception:  # noqa: BLE001
            pass
    return total


def _configured_paper_nav() -> Decimal:
    """Fallback total equity when no broker balances and no DB snapshot."""
    orch = _get_orchestrator()
    tl = getattr(orch, "_trading_loop", None) if orch is not None else None
    if tl is not None:
        try:
            return Decimal(str(getattr(tl, "portfolio_value", 0)))
        except Exception:  # noqa: BLE001
            pass
    try:
        return Decimal(str(os.getenv("PORTFOLIO_VALUE", "100000")))
    except Exception:  # noqa: BLE001
        return Decimal("0")


@app.get("/pnl")
async def get_pnl(session_factory=Depends(_session_factory)):
    today = date.today().isoformat()
    async with session_factory() as session:
        today_q = await session.execute(select(DailyPnL).where(DailyPnL.date == today).limit(1))
        today_row = today_q.scalars().first()
        agg_q = await session.execute(
            select(
                func.coalesce(func.sum(DailyPnL.realised_pnl), 0),
                func.coalesce(func.sum(DailyPnL.unrealised_pnl), 0),
                func.coalesce(func.sum(DailyPnL.total_fees), 0),
                func.coalesce(func.sum(DailyPnL.trade_count), 0),
            )
        )
        agg = agg_q.one()

    mtm_unrealised = await _compute_live_unrealised_mtm(session_factory)
    db_today_unrealised = Decimal(str(today_row.unrealised_pnl if today_row else 0))
    today_unrealised = mtm_unrealised if mtm_unrealised != 0 else db_today_unrealised

    db_value = today_row.portfolio_value if today_row and today_row.portfolio_value else Decimal(0)
    live_value = await _live_portfolio_value()
    configured_nav = _configured_paper_nav()
    # Headline = best available total: brokers, DB snapshot, or PORTFOLIO_VALUE / trading_loop setting.
    # Stale DailyPnL (e.g. old scaled £46k) must not hide a higher configured or live equity.
    display_value = max(live_value, db_value, configured_nav)
    if APP_ENV != "live":
        display_value = max(Decimal(0), display_value + today_unrealised)

    orch = _get_orchestrator()
    cap_pct = 1.0
    if orch is not None:
        try:
            cap_pct = float(getattr(orch, "capital_pct", 1.0))
        except (TypeError, ValueError):
            cap_pct = 1.0
    cap_pct = max(0.0, min(1.0, cap_pct))
    tradable_value = display_value * Decimal(str(cap_pct))

    return {
        "today": {
            "realised": _decimal_str(today_row.realised_pnl if today_row else 0),
            "unrealised": _decimal_str(today_unrealised),
            "fees": _decimal_str(today_row.total_fees if today_row else 0),
            "trades": int(today_row.trade_count if today_row else 0),
            "portfolio_value": _decimal_str(display_value),
            "tradable_capital": _decimal_str(tradable_value),
            "capital_allocation_pct": cap_pct,
        },
        "all_time": {
            "realised": _decimal_str(agg[0]),
            "unrealised": _decimal_str(agg[1]),
            "fees": _decimal_str(agg[2]),
            "trades": int(agg[3] or 0),
        },
    }


@app.get("/pnl/history")
async def get_pnl_history(
    limit: int = Query(90, ge=1, le=365),
    session_factory=Depends(_session_factory),
):
    async with session_factory() as session:
        q = await session.execute(select(DailyPnL).order_by(DailyPnL.date.desc()).limit(limit))
        rows = list(q.scalars().all())
    rows.reverse()
    return {
        "history": [
            {
                "date": r.date,
                "realised": _decimal_str(r.realised_pnl),
                "unrealised": _decimal_str(r.unrealised_pnl),
                "fees": _decimal_str(r.total_fees),
                "trades": int(r.trade_count or 0),
                "portfolio_value": _decimal_str(r.portfolio_value),
            }
            for r in rows
        ]
    }


def _command_row_dict(r) -> dict[str, Any]:
    return {
        "id": r.id,
        "type": r.command_type,
        "payload": r.payload or {},
        "status": r.status,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "claimed_at": r.claimed_at.isoformat() if r.claimed_at else None,
        "processed_at": r.processed_at.isoformat() if r.processed_at else None,
        "error": r.error,
        "source": r.source,
    }


@app.get("/control/commands")
async def get_control_commands(limit: int = Query(50, ge=1, le=500), bus: CommandBus = Depends(_command_bus)):
    rows = await bus.get_recent_commands(limit=limit)
    return {"commands": [_command_row_dict(r) for r in rows]}


@app.get("/control/commands/{command_id}")
async def get_control_command(command_id: int, bus: CommandBus = Depends(_command_bus)):
    row = await bus.get_command(command_id)
    if row is None:
        raise HTTPException(status_code=404, detail="command not found")
    return _command_row_dict(row)


@app.get("/risk/parameters")
async def get_risk_parameters():
    pm = ParameterManager(enable_db_logging=False)
    bus = getattr(app.state, "command_bus", None)
    out = await merge_risk_parameters_for_api(pm, bus)
    return {"parameters": out}


class _DashboardLoginBody(BaseModel):
    password: str


@app.post("/auth/dashboard/login")
async def dashboard_login(body: _DashboardLoginBody):
    pwd = os.getenv("DASHBOARD_PASSWORD", "").strip()
    tok = os.getenv("DASHBOARD_READ_TOKEN", "").strip()
    if not pwd:
        raise HTTPException(status_code=503, detail="DASHBOARD_PASSWORD not configured")
    if body.password != pwd:
        raise HTTPException(status_code=401, detail="invalid password")
    if not tok:
        raise HTTPException(status_code=503, detail="DASHBOARD_READ_TOKEN not configured")
    return {"ok": True, "token": tok}


@app.post("/risk/parameters/{name}")
async def set_risk_parameter(
    name: str,
    payload: dict[str, Any],
    _: None = Depends(_require_mutation_token),
    bus: CommandBus = Depends(_command_bus),
):
    value = payload.get("value")
    reason = str(payload.get("reason", "api override"))
    if value is None:
        raise HTTPException(status_code=400, detail="value required")
    cmd_id = await bus.enqueue("set_parameter", {"name": name, "value": value, "reason": reason}, source="api")
    return {"command_id": cmd_id, "parameter": name, "value": value}


@app.post("/kill")
async def activate_kill_switch(
    _: None = Depends(_require_mutation_token),
):
    """Immediately halt all trading."""
    bus = getattr(app.state, "command_bus", None)
    if bus is not None:
        cmd_id = await bus.enqueue("kill", {}, source="api")
        return {"kill_switch": True, "command_id": cmd_id, "message": "Kill command enqueued"}
    # fallback for single-process mode
    risk_engine = get_risk_engine()
    execution_engine = get_execution_engine()
    if risk_engine is None:
        raise HTTPException(status_code=503, detail="Risk engine not registered")
    risk_engine.kill()
    if execution_engine is not None:
        await execution_engine.cancel_all()
    return {"kill_switch": True, "message": "Kill switch activated; open orders cancelled"}


@app.post("/kill/reset")
async def reset_kill_switch(
    _: None = Depends(_require_mutation_token),
):
    """Re-enable trading after kill switch."""
    bus = getattr(app.state, "command_bus", None)
    if bus is not None:
        cmd_id = await bus.enqueue("reset_kill", {}, source="api")
        return {"kill_switch": False, "command_id": cmd_id, "message": "Reset command enqueued"}
    risk_engine = get_risk_engine()
    if risk_engine is None:
        raise HTTPException(status_code=503, detail="Risk engine not registered")
    risk_engine.reset_kill()
    return {"kill_switch": False, "message": "Kill switch reset"}


@app.post("/strategy/{name}/toggle")
async def toggle_strategy(
    name: str,
    payload: dict[str, Any],
    _: None = Depends(_require_mutation_token),
    bus: CommandBus = Depends(_command_bus),
):
    enabled = bool(payload.get("enabled", True))
    state_key = f"strategy.enabled.{name}"
    await bus.set_state(state_key, enabled)
    cmd_id = await bus.enqueue("toggle_strategy", {"name": name, "enabled": enabled}, source="api")
    return {"strategy": name, "enabled": enabled, "command_id": cmd_id}


# ── System orchestrator endpoints (one-button control) ────────────────────────


@app.get("/system/status")
async def system_status():
    """Full system status including state, brokers, infrastructure."""
    orch = _get_orchestrator()
    if orch is None:
        return {
            "state": "off",
            "paper_mode": APP_ENV != "live",
            "active_brokers": [],
            "brokers": {},
            "infrastructure": {},
            "trading": {"running": False},
            "errors": ["Orchestrator not initialized"],
            "pipeline_running": False,
        }
    return orch.status()


@app.post("/system/start")
async def system_start():
    """Start the entire trading system (one-button ON)."""
    orch = _get_orchestrator()
    if orch is None:
        raise HTTPException(status_code=503, detail="Orchestrator not available")
    result = await orch.start()
    return result


@app.post("/system/stop")
async def system_stop():
    """Stop the entire trading system (one-button OFF)."""
    orch = _get_orchestrator()
    if orch is None:
        raise HTTPException(status_code=503, detail="Orchestrator not available")
    result = await orch.stop()
    return result


@app.put("/system/capital-allocation")
async def set_capital_allocation(body: dict):
    """Set the fraction of total equity used for order sizing and sleeve risk limits (0.0–1.0)."""
    orch = _get_orchestrator()
    if orch is None:
        raise HTTPException(status_code=503, detail="Orchestrator not available")
    pct = float(body.get("pct", 1.0))
    if not (0.0 <= pct <= 1.0):
        raise HTTPException(status_code=400, detail="pct must be between 0 and 1")
    orch.set_capital_pct(pct)
    return {"capital_pct": orch.capital_pct}


@app.get("/system/capital-allocation")
async def get_capital_allocation():
    """Get the current capital allocation fraction."""
    orch = _get_orchestrator()
    if orch is None:
        return {"capital_pct": 1.0}
    return {"capital_pct": orch.capital_pct}


@app.websocket("/ws")
async def ws_updates(ws: WebSocket):
    token = ws.query_params.get("token") or ws.query_params.get("dashboard_token")
    read_tok = os.getenv("DASHBOARD_READ_TOKEN", "").strip()
    if read_tok and token != read_tok:
        await ws.close(code=4401)
        return
    await ws.accept()
    bus = getattr(app.state, "command_bus", None)
    sf = getattr(app.state, "db_session_factory", None)
    try:
        while True:
            status = await get_status()
            events = await gather_ws_events(bus, sf)
            orch = _get_orchestrator()
            sys_status = orch.status() if orch else {"state": "off"}
            await ws.send_json(
                {
                    "type": "tick",
                    "payload": {
                        "status": status,
                        "system": sys_status,
                        "events": events,
                    },
                    "ts": datetime.now(timezone.utc).isoformat(),
                }
            )
            await asyncio.sleep(2)
    except WebSocketDisconnect:
        return


_VALID_MODES = frozenset({"defender", "trader", "hunter"})
_MODE_RUNTIME_FILE = Path("data/runtime/active_mode.json")


def _load_risk_modes() -> dict:
    try:
        import yaml as _yaml
        p = Path("config/risk_modes.yaml")
        if p.is_file():
            return _yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001
        pass
    return {}


def _read_active_mode() -> str:
    try:
        if _MODE_RUNTIME_FILE.is_file():
            return str(json.loads(_MODE_RUNTIME_FILE.read_text(encoding="utf-8")).get("mode", "trader"))
    except Exception:  # noqa: BLE001
        pass
    return "trader"


def _write_active_mode(mode: str) -> None:
    try:
        _MODE_RUNTIME_FILE.parent.mkdir(parents=True, exist_ok=True)
        _MODE_RUNTIME_FILE.write_text(json.dumps({"mode": mode}), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


@app.get("/system/mode")
async def get_system_mode():
    mode = _read_active_mode()
    modes_cfg = _load_risk_modes()
    info = modes_cfg.get(mode, {})
    return {
        "mode": mode,
        "label": info.get("label", mode.capitalize()),
        "description": info.get("description", ""),
        "available_modes": [
            {
                "id": m,
                "label": modes_cfg[m].get("label", m.capitalize()),
                "description": modes_cfg[m].get("description", ""),
            }
            for m in ["defender", "trader", "hunter"]
            if m in modes_cfg
        ],
    }


class _ModeBody(BaseModel):
    mode: str


@app.post("/system/mode")
async def set_system_mode(body: _ModeBody):
    mode = body.mode.lower().strip()
    if mode not in _VALID_MODES:
        raise HTTPException(status_code=400, detail=f"mode must be one of: {sorted(_VALID_MODES)}")

    modes_cfg = _load_risk_modes()
    profile = modes_cfg.get(mode)
    if not profile:
        raise HTTPException(status_code=500, detail="risk_modes.yaml missing or corrupt")

    # Apply overrides to the live risk engine (in-process mutation — safe, auditable)
    risk_engine = get_risk_engine()
    applied: dict[str, Any] = {}
    if risk_engine is not None:
        for key, value in profile.items():
            if key in ("label", "description"):
                continue
            try:
                risk_engine.config[key] = value
                applied[key] = value
            except Exception:  # noqa: BLE001
                pass

    _write_active_mode(mode)
    return {
        "mode": mode,
        "label": profile.get("label", mode.capitalize()),
        "applied": applied,
        "live_engine_updated": risk_engine is not None,
    }


# ---------------------------------------------------------------------------
# Serve the React UI from ui/dist (must be LAST so API routes take priority)
# ---------------------------------------------------------------------------
_UI_DIR = Path(__file__).resolve().parent.parent / "ui" / "dist"
if _UI_DIR.is_dir():
    _index_html = _UI_DIR / "index.html"

    @app.get("/{full_path:path}", include_in_schema=False)
    async def _spa_fallback(full_path: str):  # noqa: ARG001
        """Serve index.html for any path not matched by the API (SPA routing)."""
        from starlette.responses import FileResponse

        if (_UI_DIR / full_path).is_file():
            return FileResponse(_UI_DIR / full_path)
        if _index_html.is_file():
            return FileResponse(_index_html)
        raise HTTPException(404)
