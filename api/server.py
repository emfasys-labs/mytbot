"""FastAPI backend for M7 control plane + dashboard."""

from __future__ import annotations

import asyncio
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
from storage.models import AIOutputLog, AnomalyLog, DailyPnL, NewsHeadline, OrderLog, PositionLog, SignalLog, ThesisLog

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
    connected_brokers = list(getattr(execution_engine, "_brokers", {}).keys()) if execution_engine is not None else []
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
    return {
        "positions": [
            {
                "timestamp": r.timestamp.isoformat() if r.timestamp else None,
                "symbol": r.symbol,
                "broker": r.broker,
                "quantity": _decimal_str(r.quantity),
                "avg_entry_price": _decimal_str(r.avg_entry_price),
                "current_price": _decimal_str(r.current_price),
                "unrealised_pnl": _decimal_str(r.unrealised_pnl),
                "asset_class": r.asset_class,
            }
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
    """Sum net-liquidation across all connected brokers.

    Each broker returns a list of Balance objects (one per currency).
    We take the largest total per broker as the net-liquidation proxy
    (IBKR returns BASE; crypto brokers return USDT/USD).
    """
    orch = _get_orchestrator()
    if orch is None:
        return Decimal(0)
    bm = getattr(orch, "_broker_manager", None)
    if bm is None:
        return Decimal(0)
    total = Decimal(0)
    for _name, adapter in bm.adapters.items():
        try:
            balances = await adapter.get_balance()
            if not balances:
                continue
            best = max(balances, key=lambda b: b.total)
            if best.total > 0:
                total += best.total
        except Exception:
            pass
    return total


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

    db_value = today_row.portfolio_value if today_row and today_row.portfolio_value else Decimal(0)
    if db_value <= 0:
        db_value = await _live_portfolio_value()

    return {
        "today": {
            "realised": _decimal_str(today_row.realised_pnl if today_row else 0),
            "unrealised": _decimal_str(today_row.unrealised_pnl if today_row else 0),
            "fees": _decimal_str(today_row.total_fees if today_row else 0),
            "trades": int(today_row.trade_count if today_row else 0),
            "portfolio_value": _decimal_str(db_value),
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
    """Set the fraction of capital exposed for trading (0.0–1.0)."""
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
