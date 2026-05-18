"""FastAPI backend for M7 control plane + dashboard."""

from __future__ import annotations

import asyncio
import json
import os
import re
import time as _time
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

# Same as runners: uvicorn does not load .env automatically — without this,
# POSTGRES_* and API_CONTROL_TOKEN are missing when starting `uvicorn api.server:app`.
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from fastapi import Body, Depends, FastAPI, Header, HTTPException, Query, Response, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import func, select, text
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from api.dashboard_layer import gather_ws_events, log_cors_live_warning, merge_risk_parameters_for_api, verify_dashboard_token
from api.pnl_periods import (
    aggregate_daily_pnl_range,
    equity_max_drawdown_pct,
    merge_live_today_unrealised_into_period,
    month_to_date_range,
    week_to_date_range,
    win_rate_from_daily_rows,
)
from core.broker_paper import NO_NATIVE_PAPER_POSITION_BROKERS
from control.command_bus import CAPITAL_ALLOCATION_STATE_KEY, CommandBus
from data.news_quality import is_displayable_news_item
from system.dashboard_publish import DASHBOARD_SNAPSHOT_KEY
from system.portfolio_equity import live_portfolio_snapshot, live_portfolio_value
from portfolio.global_edge_coordinator import cash_factor_for_asset_class
from control.runtime import get_execution_engine, get_risk_engine
from control.startup_validation import validate_startup_env
from risk.parameters import ParameterManager
from storage.db import bind_app_database, clear_app_database_bind, dispose_engine, init_async_database
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


def _pick_strongest_news_log_per_symbol(news_rows: list[Any]) -> dict[str, Any]:
    """
    For each symbol, keep the log row with the largest |score| in the fetched window.

    The AI pipeline persists one row per symbol every cycle; the latest row is often 0.0 when
    no headline matched. Without this merge, dashboard 'news movers' would hide prior non-zero
    scores for the same symbol.
    """
    by_symbol: dict[str, Any] = {}
    for r in news_rows:
        s = (getattr(r, "symbol", None) or "").strip().upper()
        if not s:
            continue
        if not _news_row_matches_logged_symbol(r):
            continue
        try:
            sc = float(r.score) if r.score is not None else 0.0
        except (TypeError, ValueError):
            sc = 0.0
        prev = by_symbol.get(s)
        psc = float(prev.score) if prev is not None and prev.score is not None else 0.0
        if prev is None or abs(sc) > abs(psc):
            by_symbol[s] = r
    return by_symbol


def _float_or_zero(v: Any) -> float:
    try:
        return float(v) if v is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def _metadata_float(metadata: dict[str, Any] | None, key: str) -> float | None:
    """Parse optional scores from persisted JSON metadata (float, Decimal, int, or str)."""
    if not metadata or key not in metadata:
        return None
    raw = metadata[key]
    if raw is None:
        return None
    try:
        if isinstance(raw, bool):
            return None
        if isinstance(raw, Decimal):
            return float(raw)
        if isinstance(raw, (int, float)):
            return float(raw)
        return float(str(raw).strip())
    except (TypeError, ValueError):
        return None


def _signal_news_impact_source(signal_row: Any, attribution: list[dict[str, Any]]) -> str:
    if attribution:
        return "headline"
    metadata = getattr(signal_row, "metadata_", None) or {}
    ai_news_score = _metadata_float(metadata, "ai_news_score")
    if ai_news_score is not None and ai_news_score != 0.0:
        return "ai_news"
    accumulator_score = _metadata_float(metadata, "accumulator_score")
    if accumulator_score is not None and accumulator_score != 0.0:
        return "accumulator"
    if _float_or_zero(getattr(signal_row, "news_score", None)) != 0.0:
        return "signal"
    return "none"


_EXPLICIT_TICKER_RE = re.compile(r"\$([A-Z]{1,8})\b")
# Venue-prefixed symbols in execution logs (``ibkr:AAPL``, ``kraken:ETH-USD``);
# AIOutputLog.news rows use native tickers without the router prefix.
_BROKER_PREFIX_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,19}:")
_BROAD_MARKET_TICKERS = frozenset(
    {
        "SPY",
        "QQQ",
        "IWM",
        "DIA",
        "TLT",
        "GLD",
        "SLV",
        "USO",
        "DXY",
        "VIX",
        "BTC",
        "ETH",
    }
)
_MARKET_WIDE_NEWS_EVENTS = frozenset({"macro", "geopolitical", "geopolitics", "crypto"})


def _canonical_symbol_for_news_lookup(symbol: str) -> str:
    s = (symbol or "").strip().upper()
    if _BROKER_PREFIX_RE.match(s) and ":" in s:
        return s.split(":", 1)[1].strip().upper()
    return s


def _news_row_headline_text(row: Any) -> str:
    payload = getattr(row, "payload", None)
    if isinstance(payload, dict):
        txt = payload.get("headline") or payload.get("title") or ""
        if txt:
            return str(txt)
    return str(getattr(row, "rationale", "") or "")


def _explicit_tickers_in_news_row(row: Any) -> set[str]:
    return {m.group(1).upper() for m in _EXPLICIT_TICKER_RE.finditer(_news_row_headline_text(row))}


def _candidate_news_rows_for_signal(symbol: str, rows: list[Any]) -> list[Any]:
    sym_u = _canonical_symbol_for_news_lookup(symbol)
    allowed = set(_alias_symbols_for_signal(sym_u))
    allowed.add(sym_u)
    out: list[Any] = []
    for row in rows:
        explicit = _explicit_tickers_in_news_row(row)
        if explicit and not (explicit & allowed):
            continue
        out.append(row)
    return out


def _news_row_matches_logged_symbol(row: Any) -> bool:
    symbol = _canonical_symbol_for_news_lookup((getattr(row, "symbol", None) or ""))
    if not symbol:
        return True
    explicit = _explicit_tickers_in_news_row(row)
    if not explicit:
        return True
    allowed = set(_alias_symbols_for_signal(symbol))
    allowed.add(symbol)
    return bool(explicit & allowed)


def _is_market_wide_news_row(row: Any) -> bool:
    explicit = _explicit_tickers_in_news_row(row)
    if explicit and not explicit.issubset(_BROAD_MARKET_TICKERS):
        return False
    event_type = str(getattr(row, "event_type", "") or "").strip().lower()
    if event_type in _MARKET_WIDE_NEWS_EVENTS:
        return True
    payload = getattr(row, "payload", None)
    if isinstance(payload, dict):
        payload_event = str(payload.get("event_type") or payload.get("category") or "").strip().lower()
        if payload_event in _MARKET_WIDE_NEWS_EVENTS:
            return True
    return False


def _signal_news_attribution(signal_row: Any, symbol_news_rows: list[Any], *, max_items: int = 2) -> list[dict[str, Any]]:
    """
    Build per-signal explainability rows from nearby AI news logs.

    Windows are deliberately wide symmetrically — the runner may persist
    ``SignalLog.timestamp`` noticeably after AI batch scoring timestamps, so
    a tight forward-only heuristic hid explainability for older dashboard rows.
    """
    ts = getattr(signal_row, "timestamp", None)
    if ts is None:
        return []
    sig_ts = ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    before_ahead = timedelta(hours=48)
    lookahead = timedelta(hours=48)
    abs_before = before_ahead.total_seconds()
    abs_after = lookahead.total_seconds()
    picked: list[tuple[float, float, Any]] = []
    for r in symbol_news_rows:
        r_ts = getattr(r, "timestamp", None)
        if r_ts is None:
            continue
        row_ts = r_ts if r_ts.tzinfo else r_ts.replace(tzinfo=timezone.utc)
        delta_s = (sig_ts - row_ts).total_seconds()
        # Symmetric lookahead / look-back (default ±48h) around the logged signal time.
        if delta_s < -abs_after or delta_s > abs_before:
            continue
        score = _float_or_zero(getattr(r, "score", None))
        if score == 0.0:
            continue
        abs_delta = abs(delta_s)
        picked.append((abs_delta, -abs(score), r))
    if not picked:
        return []
    picked.sort(key=lambda x: (x[0], x[1]))
    out: list[dict[str, Any]] = []
    for _, _, r in picked[:max_items]:
        payload = r.payload if isinstance(getattr(r, "payload", None), dict) else {}
        headline = str(
            payload.get("headline")
            or payload.get("title")
            or payload.get("rationale")
            or ""
        ).strip()
        source = str(
            payload.get("provider")
            or payload.get("source")
            or getattr(r, "source", None)
            or ""
        ).strip()
        out.append(
            {
                "headline": headline,
                "source": source or None,
                "score": _float_or_zero(getattr(r, "score", None)),
                "event_type": getattr(r, "event_type", None),
                "scored_at": r.timestamp.isoformat() if getattr(r, "timestamp", None) else None,
            }
        )
    return out


def _alias_symbols_for_signal(symbol: str) -> list[str]:
    s = _canonical_symbol_for_news_lookup(symbol or "")
    alias_map: dict[str, list[str]] = {
        "ES": ["SPY", "ES", "SPX"],
        "NQ": ["QQQ", "NQ", "NDX"],
        "YM": ["DIA", "YM", "DJI"],
        "CL": ["USO", "CL", "OIL", "WTI"],
        "GC": ["GLD", "GC", "GOLD", "XAUUSD", "XAU"],
        "SI": ["SLV", "SI", "SILVER", "XAGUSD", "XAG"],
        "EURUSD": ["EURUSD", "EUR", "USD", "DXY"],
        "USDJPY": ["USDJPY", "USD", "JPY", "DXY"],
        "GBPUSD": ["GBPUSD", "GBP", "USD", "DXY"],
        "AUDUSD": ["AUDUSD", "AUD", "USD", "DXY"],
        "USDCHF": ["USDCHF", "USD", "CHF", "DXY"],
        "USDCAD": ["USDCAD", "USD", "CAD", "DXY"],
    }
    if s.endswith("=F"):
        s = s[:-2]
    return alias_map.get(s, [s])


def _news_lookup_symbols_for_signals(symbols: list[str]) -> list[str]:
    """Expand signal symbols to the direct + alias symbols used by AI news logs."""
    out: dict[str, None] = {}
    for sym in symbols:
        s = _canonical_symbol_for_news_lookup(sym or "")
        if not s:
            continue
        out[s] = None
        for a in _alias_symbols_for_signal(s):
            aa = (a or "").strip().upper()
            if aa:
                out[aa] = None
    return list(out.keys())


def _is_public_spa_get(path: str, method: str) -> bool:
    """Browser navigation to / has no custom headers — allow loading the SPA shell and Vite assets only."""
    if method != "GET":
        return False
    if path in ("/", "/index.html"):
        return True
    if path.startswith("/assets/"):
        return True
    return False


class _DashboardReadMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):  # noqa: ANN001
        if request.scope.get("type") == "websocket":
            return await call_next(request)
        # TestClient runs with the developer's .env; read-token middleware would block
        # most API tests. Opt-in bypass via conftest `PYTEST_API_DISABLE_READ_MIDDLEWARE`;
        # tests that assert on read protection call `monkeypatch.delenv(...)` first.
        if os.getenv("PYTEST_API_DISABLE_READ_MIDDLEWARE", "").strip().lower() in ("1", "true", "yes", "on"):
            return await call_next(request)
        if request.method == "OPTIONS":
            return await call_next(request)
        path = request.url.path
        if path in _EXEMPT_READ_AUTH or path.startswith("/docs") or path.startswith("/redoc"):
            return await call_next(request)
        if path == "/auth/dashboard/login" and request.method == "POST":
            return await call_next(request)
        if _is_public_spa_get(path, request.method):
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


def _require_dashboard_read_token_if_live() -> None:
    """In live mode, refuse to start the API without a dashboard read token (optional escape hatch for dev)."""
    if os.getenv("PYTEST_CURRENT_TEST"):
        return
    if os.getenv("ALLOW_MISSING_DASHBOARD_READ_TOKEN_LIVE", "").strip().lower() in ("1", "true", "yes"):
        return
    if os.getenv("APP_ENV", "paper").strip().lower() == "live":
        if not os.getenv("DASHBOARD_READ_TOKEN", "").strip():
            raise RuntimeError(
                "APP_ENV=live requires DASHBOARD_READ_TOKEN for dashboard/API read protection. "
                "Set it in .env or set ALLOW_MISSING_DASHBOARD_READ_TOKEN_LIVE=1 for local dev only."
            )


@app.on_event("startup")
async def _startup() -> None:
    log_cors_live_warning()
    _require_dashboard_read_token_if_live()
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
    app.state.db_rebind_lock = asyncio.Lock()
    bind_app_database(engine, session_factory)


@app.on_event("shutdown")
async def _shutdown() -> None:
    clear_app_database_bind()
    await dispose_engine(getattr(app.state, "db_engine", None))


def _require_mutation_token(x_control_token: str | None = Header(default=None, alias="X-Control-Token")) -> None:
    if not MUTATION_TOKEN:
        return
    if x_control_token != MUTATION_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid control token")


async def _ensure_database_bind() -> None:
    """
    Re-bind the API database lazily if startup ran before Docker/Postgres was ready.

    ``python run.py`` starts the API before the operator presses Start, while the
    orchestrator starts Docker later. A one-shot DB bind at FastAPI startup can
    therefore leave ``command_bus`` permanently unavailable even after Postgres
    is healthy. Dashboard reads call this helper before failing so late
    infrastructure comes online without requiring a full app restart.
    """
    if getattr(app.state, "db_session_factory", None) is not None:
        return
    if os.getenv("PYTEST_CURRENT_TEST"):
        return
    lock = getattr(app.state, "db_rebind_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        app.state.db_rebind_lock = lock
    async with lock:
        if getattr(app.state, "db_session_factory", None) is not None:
            return
        engine, session_factory = await init_async_database()
        app.state.db_engine = engine
        app.state.db_session_factory = session_factory
        app.state.command_bus = CommandBus(session_factory) if session_factory is not None else None
        bind_app_database(engine, session_factory)


async def _session_factory():
    await _ensure_database_bind()
    sf = getattr(app.state, "db_session_factory", None)
    if sf is None:
        raise HTTPException(status_code=503, detail="Database unavailable")
    return sf


async def _optional_session_factory():
    """DB session factory when present; None if app has no database (e.g. light tests)."""
    await _ensure_database_bind()
    return getattr(app.state, "db_session_factory", None)


async def _command_bus() -> CommandBus:
    await _ensure_database_bind()
    bus = getattr(app.state, "command_bus", None)
    if bus is None:
        raise HTTPException(status_code=503, detail="Command bus unavailable")
    return bus


def _decimal_str(v: Any) -> str:
    if v is None:
        return "0"
    d = Decimal(str(v))
    if d == 0:
        return "0"
    return str(d)


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


async def _live_broker_prices(rows: list[PositionLog]) -> dict[str, Decimal]:
    """Best-effort live last-price lookup via broker adapters.

    Hourly FeatureSnapshot bars are far too stale to drive a live equity
    curve — brokers expose a real-time `get_last_price(symbol)` which is the
    freshest source we have. Query all connected adapters per symbol and pick
    a deterministic median positive quote. The old "first non-zero wins" race
    made Book unrealised totals snap flat when a stale/default quote returned
    a few milliseconds before the genuine market-data source.
    """
    orch = _get_orchestrator()
    if orch is None:
        return {}
    bm = getattr(orch, "_broker_manager", None)
    if bm is None or not rows:
        return {}

    adapters = list(bm.adapters.items())
    if not adapters:
        return {}

    async def _probe(broker_name: str, adapter, sym: str) -> tuple[str, Decimal]:
        try:
            px = await asyncio.wait_for(adapter.get_last_price(sym), timeout=1.5)
        except Exception:  # noqa: BLE001
            return broker_name, Decimal(0)
        if px is None:
            return broker_name, Decimal(0)
        try:
            d = Decimal(str(px))
        except Exception:  # noqa: BLE001
            return broker_name, Decimal(0)
        return broker_name, d if d > 0 else Decimal(0)

    symbol_rows: dict[str, list[PositionLog]] = {}
    for r in rows:
        sym = str(r.symbol or "").strip().upper()
        if sym:
            symbol_rows.setdefault(sym, []).append(r)

    async def _one(sym: str) -> tuple[str, Decimal]:
        tasks = [asyncio.create_task(_probe(name, a, sym)) for name, a in adapters]
        done: set[asyncio.Task] = set()
        pending: set[asyncio.Task] = set(tasks)
        try:
            done, pending = await asyncio.wait(tasks, timeout=1.8)
            quotes: list[Decimal] = []
            for t in done:
                try:
                    _broker, px = t.result()
                except Exception:  # noqa: BLE001
                    continue
                if px > 0:
                    quotes.append(px)
            if quotes:
                quotes.sort()
                mid = len(quotes) // 2
                if len(quotes) % 2 == 1:
                    return sym, quotes[mid]
                return sym, (quotes[mid - 1] + quotes[mid]) / Decimal("2")
        finally:
            for t in pending:
                t.cancel()
        return sym, Decimal(0)

    results = await asyncio.gather(*(_one(sym) for sym in symbol_rows), return_exceptions=True)
    out: dict[str, Decimal] = {}
    for res in results:
        if isinstance(res, Exception):
            continue
        sym, px = res
        if sym and px > 0:
            out[sym] = px
    return out


async def _latest_position_log_rows(
    session,
    *,
    limit: int | None = None,
    open_only: bool = False,
) -> list[PositionLog]:
    """
    Return the latest ledger row per broker-symbol.

    ``positions`` is append-only. A zero-quantity tombstone is a meaningful
    latest row and must suppress older non-zero rows for that same broker-symbol,
    but it should not appear in the open-position book.
    """
    ranked = (
        select(
            PositionLog.id.label("id"),
            func.row_number()
            .over(
                partition_by=(PositionLog.broker, PositionLog.symbol),
                order_by=(PositionLog.timestamp.desc(), PositionLog.id.desc()),
            )
            .label("rn"),
        )
        .subquery()
    )
    stmt = (
        select(PositionLog)
        .join(ranked, PositionLog.id == ranked.c.id)
        .where(ranked.c.rn == 1)
        .order_by(PositionLog.symbol.asc(), PositionLog.broker.asc())
    )
    if open_only:
        stmt = stmt.where(func.abs(PositionLog.quantity) > Decimal("0.00000001"))
    if limit is not None:
        stmt = stmt.limit(limit)
    q = await session.execute(stmt)
    return list(q.scalars().all())


def _position_log_payload(
    r: PositionLog,
    *,
    current_price: Decimal | None = None,
) -> dict[str, Any]:
    qty = Decimal(str(r.quantity or 0))
    avg = Decimal(str(r.avg_entry_price or 0))
    current = current_price if current_price is not None else Decimal(str(r.current_price or 0))
    return {
        "timestamp": r.timestamp.isoformat() if r.timestamp else None,
        "symbol": r.symbol,
        "broker": r.broker,
        "quantity": _decimal_str(qty),
        "avg_entry_price": _decimal_str(avg),
        "current_price": _decimal_str(current),
        "unrealised_pnl": _decimal_str((current - avg) * qty),
        "asset_class": r.asset_class,
    }


async def _live_broker_positions(limit: int) -> list[dict[str, Any]]:
    """Return current positions directly from connected broker adapters.

    ``PositionLog`` is an audit/cache table; the broker is the authoritative
    source for what is actually open. A partial latest DB snapshot can otherwise
    hide still-open positions from another broker.
    """
    orch = _get_orchestrator()
    bm = getattr(orch, "_broker_manager", None) if orch is not None else None
    adapters = list(getattr(bm, "adapters", {}).items()) if bm is not None else []
    if not adapters:
        return []

    async def _one(broker_name: str, adapter: Any) -> list[dict[str, Any]]:
        try:
            positions = await asyncio.wait_for(adapter.get_positions(), timeout=8.0)
        except Exception:  # noqa: BLE001
            return []
        out: list[dict[str, Any]] = []
        ts = datetime.now(timezone.utc).isoformat()
        for p in positions or []:
            try:
                qty = Decimal(str(getattr(p, "quantity", 0) or 0))
                avg = Decimal(str(getattr(p, "avg_entry_price", 0) or 0))
                current = Decimal(str(getattr(p, "current_price", 0) or 0))
                pnl_raw = getattr(p, "unrealised_pnl", None)
                pnl = Decimal(str(pnl_raw)) if pnl_raw is not None else (current - avg) * qty
            except Exception:  # noqa: BLE001
                continue
            if qty == 0:
                continue
            asset_class = getattr(p, "asset_class", "")
            if hasattr(asset_class, "value"):
                asset_class = asset_class.value
            out.append(
                {
                    "timestamp": ts,
                    "symbol": str(getattr(p, "symbol", "") or "").strip().upper(),
                    "broker": str(getattr(p, "broker", broker_name) or broker_name).strip().lower(),
                    "quantity": _decimal_str(qty),
                    "avg_entry_price": _decimal_str(avg),
                    "current_price": _decimal_str(current),
                    "unrealised_pnl": _decimal_str(pnl),
                    "asset_class": str(asset_class or "").strip().lower(),
                }
            )
        return out

    results = await asyncio.gather(
        *(_one(name, adapter) for name, adapter in adapters),
        return_exceptions=True,
    )
    rows: list[dict[str, Any]] = []
    for res in results:
        if isinstance(res, list):
            rows.extend(res)
    rows.sort(key=lambda r: (r.get("symbol") or "", r.get("broker") or ""))
    return rows[:limit]


async def _merge_synthetic_paper_positions_from_log(
    session_factory,
    live_rows: list[dict[str, Any]],
    *,
    limit: int,
) -> list[dict[str, Any]]:
    """Append simulated paper-ledger legs that broker ``get_positions()`` lacks."""

    keys_seen: set[tuple[str, str]] = set()
    for r in live_rows:
        try:
            b = str(r.get("broker", "")).strip().lower()
            s = str(r.get("symbol", "")).strip().upper()
            if b and s:
                keys_seen.add((b, s))
        except Exception:  # noqa: BLE001
            continue

    async with session_factory() as session:
        pl_rows = await _latest_position_log_rows(session, open_only=True)
        pl_rows = _filter_position_logs_to_current_nav_brokers(pl_rows)
        if not pl_rows:
            return live_rows
        sym_list = [str(r.symbol).strip().upper() for r in pl_rows if r.symbol]
        prices = await _latest_feature_prices(session, sym_list)
    live_px = await _live_broker_prices(pl_rows)

    ts = datetime.now(timezone.utc).isoformat()
    extra: list[dict[str, Any]] = []
    for r in pl_rows:
        b = str(r.broker).strip().lower()
        sym = str(r.symbol).strip().upper()
        if not b or not sym:
            continue
        if (b, sym) in keys_seen:
            continue
        try:
            qty = Decimal(str(r.quantity or 0))
            avg = Decimal(str(r.avg_entry_price or 0))
        except Exception:  # noqa: BLE001
            continue
        if qty == 0:
            continue
        px = live_px.get(sym) or prices.get(sym) or Decimal(str(r.current_price or 0))
        if px <= 0 and avg > 0:
            px = avg
        pnl = (px - avg) * qty if avg > 0 else Decimal(0)
        ac = str(r.asset_class or "crypto").strip().lower()
        extra.append(
            {
                "timestamp": ts,
                "symbol": sym,
                "broker": b,
                "quantity": _decimal_str(qty),
                "avg_entry_price": _decimal_str(avg),
                "current_price": _decimal_str(px),
                "unrealised_pnl": _decimal_str(pnl),
                "asset_class": ac,
            }
        )

    merged = live_rows + extra
    merged.sort(key=lambda row: (row.get("symbol") or "", row.get("broker") or ""))
    return merged[:limit]


async def _live_broker_unrealised_total() -> Decimal:
    rows = await _live_broker_positions(500)
    rows = _filter_rows_to_current_nav_brokers(rows)
    total = Decimal(0)
    for row in rows:
        try:
            total += Decimal(str(row.get("unrealised_pnl", "0") or "0"))
        except Exception:  # noqa: BLE001
            continue
    return total


async def _compute_live_unrealised_mtm(session_factory) -> Decimal:
    """Mark latest position snapshot to the freshest price available.

    Price priority (highest freshness first):
      1. Broker live `get_last_price` (real-time tick / snapshot)
      2. FeatureSnapshot latest close (hourly bar — minutes to an hour stale)
      3. PositionLog.current_price (last persisted snapshot)
      4. Average entry price (no movement — effectively zero unrealised)
    """
    if APP_ENV == "live":
        live_total = await _live_broker_unrealised_total()
        if live_total != 0:
            return live_total

    async with session_factory() as session:
        rows = await _latest_position_log_rows(session, open_only=True)
        rows = _filter_position_logs_to_current_nav_brokers(rows)
        if not rows:
            return Decimal(0)
        feature_prices = await _latest_feature_prices(session, [r.symbol for r in rows])
    # Broker lookups happen outside the DB session so we don't hold a
    # connection while waiting on network round-trips.
    live_prices = await _live_broker_prices(rows)
    total = Decimal(0)
    for r in rows:
        qty = Decimal(str(r.quantity or 0))
        avg = Decimal(str(r.avg_entry_price or 0))
        if avg <= 0 or qty == 0:
            continue
        px = live_prices.get(r.symbol)
        if px is None or px <= 0:
            px = feature_prices.get(r.symbol)
        if px is None or px <= 0:
            px = Decimal(str(r.current_price or avg))
        total += (px - avg) * qty
    return total


def _get_orchestrator():
    """Lazy-import to avoid circular deps when API is loaded standalone."""
    try:
        from system.orchestrator import Orchestrator
        return Orchestrator.get_instance()
    except Exception:
        return None


def _current_broker_coverage() -> dict[str, Any] | None:
    orch = _get_orchestrator()
    bm = getattr(orch, "_broker_manager", None) if orch is not None else None
    report = getattr(bm, "report", None) if bm is not None else None
    if report is None:
        return None
    try:
        cov = report.coverage()
    except Exception:  # noqa: BLE001
        return None
    return cov if isinstance(cov, dict) else None


def _is_paper_system_off() -> bool:
    if APP_ENV == "live":
        return False
    orch = _get_orchestrator()
    state = getattr(getattr(orch, "state", None), "value", None) if orch is not None else None
    return str(state or "off").strip().lower() == "off"


def _current_nav_broker_filter() -> set[str] | None:
    """Return current connected/balance-ready broker names when coverage is partial.

    ``None`` means no filtering is needed or coverage is unavailable. When a
    configured broker is missing, all dashboard/accounting numbers must switch
    to the same current-coverage universe; otherwise a partial NAV can be
    divided into a full historical/position book and produce nonsense leverage.
    In paper mode while the system is off, there is intentionally no connected
    NAV broker set; keep the local paper ledger visible rather than showing a
    false flat book.
    """
    cov = _current_broker_coverage()
    if not cov or bool(cov.get("full")):
        return None
    included = cov.get("included")
    if not isinstance(included, list):
        return set()
    if not included and _is_paper_system_off():
        return None
    return {str(n).strip().lower() for n in included if str(n).strip()}


def _filter_rows_to_current_nav_brokers(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    allowed = _current_nav_broker_filter()
    if allowed is None:
        return rows
    return [r for r in rows if str(r.get("broker") or "").strip().lower() in allowed]


def _filter_position_logs_to_current_nav_brokers(rows: list[PositionLog]) -> list[PositionLog]:
    allowed = _current_nav_broker_filter()
    if allowed is None:
        return rows
    return [r for r in rows if str(getattr(r, "broker", "") or "").strip().lower() in allowed]


@app.get("/healthz")
async def healthz():
    return {"ok": True, "service": "api", "ts": datetime.now(timezone.utc).isoformat()}


@app.get("/readyz")
async def readyz():
    await _ensure_database_bind()
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
    orch_status = orch.status() if orch else {}
    orch_brokers = orch_status.get("active_brokers", []) if orch else []
    connected_brokers = orch_brokers if orch_brokers else exec_connected
    loaded_strategies = list(orch_status.get("loaded_strategies") or []) if orch else []

    if loaded_strategies:
        strategies = {
            str(s.get("name", "")).strip(): bool(s.get("enabled", True))
            for s in loaded_strategies
            if isinstance(s, dict) and str(s.get("name", "")).strip()
        }
        if bus is not None:
            state_map = await bus.get_state_prefix("strategy.enabled.")
            for k, v in state_map.items():
                name = k.replace("strategy.enabled.", "", 1)
                if name in strategies:
                    strategies[name] = bool(v)

    disabled = sorted(getattr(risk_engine, "disabled_brokers", frozenset())) if risk_engine is not None else []

    return {
        "status": "running",
        "system_state": system_state,
        "mode": APP_ENV,
        "paper_mode": APP_ENV != "live",
        "kill_switch": bool(getattr(risk_engine, "is_killed", False)) if risk_engine is not None else False,
        "disabled_brokers": disabled,
        "connected_brokers": connected_brokers or orch_brokers,
        "active_strategies": strategies,
        "loaded_strategies": loaded_strategies,
        "runtime": runtime,
    }


@app.get("/positions")
async def get_positions(limit: int = Query(200, ge=1, le=500), session_factory=Depends(_session_factory)):
    live_rows = await _live_broker_positions(limit)
    live_rows = _filter_rows_to_current_nav_brokers(live_rows)
    if APP_ENV != "live" and live_rows:
        live_rows = await _merge_synthetic_paper_positions_from_log(
            session_factory, live_rows, limit=limit
        )
    if live_rows:
        src = "live_broker" if APP_ENV == "live" else "live_broker+synthetic_paper_log"
        return {"positions": live_rows, "source": src}

    async with session_factory() as session:
        rows = await _latest_position_log_rows(session, limit=limit, open_only=True)
        rows = _filter_position_logs_to_current_nav_brokers(rows)
        if not rows:
            return {"positions": [], "source": "position_log"}
        prices = await _latest_feature_prices(session, [r.symbol for r in rows])
    live_prices = await _live_broker_prices(rows)
    return {
        "positions": [
            _position_log_payload(
                r,
                current_price=live_prices.get(
                    r.symbol,
                    prices.get(r.symbol, Decimal(str(r.current_price or 0))),
                ),
            )
            for r in rows
        ],
        "source": "position_log",
    }


def _order_log_to_dict(r: OrderLog) -> dict[str, Any]:
    meta = r.instrument_metadata if isinstance(r.instrument_metadata, dict) else None
    avg_fill_price = _decimal_str(r.avg_fill_price) if r.avg_fill_price is not None else None
    reason: str | None = None
    if meta:
        for k in ("error_message", "reject_reason", "reason"):
            v = meta.get(k)
            if isinstance(v, str) and v.strip():
                reason = v.strip()
                break
    if reason is None and bool(r.paper_mode):
        broker = str(r.broker or "").strip().lower()
        if broker in {"kraken", "binance", "bybit"} and str(r.status or "").lower() == "rejected":
            reason = f"{broker} adapter has no native paper order placement; order was not sent"
    return {
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
        "avg_fill_price": avg_fill_price,
        "filled_price": avg_fill_price,
        "fee": _decimal_str(r.fee) if r.fee is not None else None,
        "paper_mode": bool(r.paper_mode),
        "metadata": meta,
        "reason": reason,
    }


def _order_position_key(row: Any) -> tuple[str, str]:
    return (
        str(getattr(row, "broker", "") or "").strip().lower(),
        str(getattr(row, "symbol", "") or "").strip().upper(),
    )


def _apply_order_fill_to_position_state(
    *,
    position_qty: Decimal,
    position_avg: Decimal,
    side: str,
    fill_qty: Decimal,
    fill_price: Decimal,
    fee: Decimal,
) -> tuple[Decimal, Decimal, dict[str, Decimal | bool | None]]:
    """Apply a fill to a signed position and return realised close P&L.

    Long closes are sells; short closes are buys. Fees are allocated
    proportionally when a fill both closes an old leg and opens a flipped leg.
    """
    side_l = str(side or "").strip().lower()
    if side_l not in {"buy", "sell"} or fill_qty <= 0 or fill_price <= 0:
        return position_qty, position_avg, {
            "closes_position": False,
            "realised_pnl_gross": None,
            "realised_pnl_fee": None,
            "realised_pnl_net": None,
            "trade_pnl_net": None,
            "closed_quantity": Decimal("0"),
        }

    signed_fill = fill_qty if side_l == "buy" else -fill_qty
    closing_qty = Decimal("0")
    gross = Decimal("0")
    if position_qty > 0 and signed_fill < 0:
        closing_qty = min(position_qty, abs(signed_fill))
        gross = (fill_price - position_avg) * closing_qty
    elif position_qty < 0 and signed_fill > 0:
        closing_qty = min(abs(position_qty), signed_fill)
        gross = (position_avg - fill_price) * closing_qty

    fee_alloc = (fee * (closing_qty / fill_qty)) if closing_qty > 0 else Decimal("0")
    net = gross - fee_alloc

    new_qty = position_qty + signed_fill
    eps = Decimal("0.00000001")
    if abs(new_qty) <= eps:
        new_qty = Decimal("0")
        new_avg = fill_price
    elif position_qty == 0 or (position_qty > 0 and signed_fill > 0) or (position_qty < 0 and signed_fill < 0):
        total_abs = abs(position_qty) + abs(signed_fill)
        new_avg = (
            ((abs(position_qty) * position_avg) + (abs(signed_fill) * fill_price)) / total_abs
            if total_abs > 0
            else fill_price
        )
    elif abs(signed_fill) < abs(position_qty):
        new_avg = position_avg
    else:
        # Fill flipped direction; the leftover is a new position opened at the fill price.
        new_avg = fill_price

    return new_qty, new_avg, {
        "closes_position": closing_qty > 0,
        "realised_pnl_gross": gross if closing_qty > 0 else None,
        "realised_pnl_fee": fee_alloc if closing_qty > 0 else None,
        "realised_pnl_net": net if closing_qty > 0 else None,
        # Opening trades realise no P&L — only a fee, which is reported on
        # the order via the dedicated ``fee`` column. Conflating the two
        # caused every new position to show as a tiny "loss" in the UI's
        # P&L column equal to its transaction cost. The right answer for
        # an opening trade is "no realised P&L yet" (None), not "-fee".
        "trade_pnl_net": (gross - fee) if closing_qty > 0 else None,
        "trade_fee_net": fee,
        "closed_quantity": closing_qty,
    }


async def _order_realised_pnl_annotations(session, rows: list[OrderLog]) -> dict[str, dict[str, Any]]:
    if not rows:
        return {}
    keys = {_order_position_key(r) for r in rows}
    keys.discard(("", ""))
    earliest = min((r.timestamp for r in rows if r.timestamp is not None), default=None)
    state: dict[tuple[str, str], tuple[Decimal, Decimal]] = {}
    if keys and earliest is not None:
        brokers = sorted({b for b, _ in keys})
        symbols = sorted({s for _, s in keys})
        q = await session.execute(
            select(PositionLog)
            .where(
                PositionLog.timestamp < earliest,
                PositionLog.broker.in_(brokers),
                PositionLog.symbol.in_(symbols),
            )
            .order_by(PositionLog.timestamp.desc(), PositionLog.id.desc())
        )
        for p in q.scalars().all():
            key = _order_position_key(p)
            if key in keys and key not in state:
                state[key] = (
                    Decimal(str(p.quantity or 0)),
                    Decimal(str(p.avg_entry_price or 0)),
                )

    annotations: dict[str, dict[str, Any]] = {}
    chronological = sorted(rows, key=lambda r: (r.timestamp or datetime.min.replace(tzinfo=timezone.utc), str(r.id)))
    for r in chronological:
        key = _order_position_key(r)
        pos_qty, pos_avg = state.get(key, (Decimal("0"), Decimal("0")))
        try:
            fill_qty = Decimal(str(r.filled_quantity if r.filled_quantity is not None else r.quantity or 0))
            fill_price = Decimal(str(r.avg_fill_price if r.avg_fill_price is not None else r.limit_price or 0))
            fee = Decimal(str(r.fee or 0))
        except Exception:  # noqa: BLE001
            fill_qty = Decimal("0")
            fill_price = Decimal("0")
            fee = Decimal("0")

        if str(r.status or "").strip().lower() not in {"filled", "partially_filled"}:
            annotations[str(r.id)] = {
                "closes_position": False,
                "trade_pnl": None,
                "trade_pnl_net": None,
                "realised_pnl": None,
                "realised_pnl_net": None,
                "realised_pnl_gross": None,
                "realised_pnl_fee": None,
                "trade_fee_net": None,
                "closed_quantity": None,
            }
            continue

        new_qty, new_avg, pnl = _apply_order_fill_to_position_state(
            position_qty=pos_qty,
            position_avg=pos_avg,
            side=str(r.side or ""),
            fill_qty=fill_qty,
            fill_price=fill_price,
            fee=fee,
        )
        state[key] = (new_qty, new_avg)
        annotations[str(r.id)] = {
            "closes_position": bool(pnl["closes_position"]),
            "trade_pnl": _decimal_str(pnl["trade_pnl_net"]) if pnl["trade_pnl_net"] is not None else None,
            "trade_pnl_net": _decimal_str(pnl["trade_pnl_net"]) if pnl["trade_pnl_net"] is not None else None,
            "realised_pnl": _decimal_str(pnl["realised_pnl_net"]) if pnl["realised_pnl_net"] is not None else None,
            "realised_pnl_net": _decimal_str(pnl["realised_pnl_net"]) if pnl["realised_pnl_net"] is not None else None,
            "realised_pnl_gross": _decimal_str(pnl["realised_pnl_gross"]) if pnl["realised_pnl_gross"] is not None else None,
            "realised_pnl_fee": _decimal_str(pnl["realised_pnl_fee"]) if pnl["realised_pnl_fee"] is not None else None,
            "trade_fee_net": _decimal_str(pnl["trade_fee_net"]) if pnl.get("trade_fee_net") is not None else None,
            "closed_quantity": _decimal_str(pnl["closed_quantity"]) if pnl["closed_quantity"] else None,
        }
    return annotations


@app.get("/orders")
async def get_orders(limit: int = Query(50, ge=1, le=500), session_factory=Depends(_session_factory)):
    async with session_factory() as session:
        q = await session.execute(select(OrderLog).order_by(OrderLog.timestamp.desc()).limit(limit))
        rows = list(q.scalars().all())
        annotations = await _order_realised_pnl_annotations(session, rows)
    orders = []
    for r in rows:
        row = _order_log_to_dict(r)
        row.update(annotations.get(str(r.id), {}))
        orders.append(row)
    return {"orders": orders}


async def _filled_order_net_pnl_for_period(
    session,
    start_day: date,
    end_day: date,
) -> tuple[Decimal, Decimal, int]:
    start_dt = datetime.combine(start_day, time.min, tzinfo=timezone.utc)
    end_dt = datetime.combine(end_day + timedelta(days=1), time.min, tzinfo=timezone.utc)
    q = await session.execute(
        select(OrderLog)
        .where(
            OrderLog.timestamp >= start_dt,
            OrderLog.timestamp < end_dt,
            OrderLog.status.in_(("filled", "partially_filled")),
        )
        .order_by(OrderLog.timestamp.desc(), OrderLog.id.desc())
    )
    rows = list(q.scalars().all())
    annotations = await _order_realised_pnl_annotations(session, rows)
    net = Decimal("0")
    fees = Decimal("0")
    for r in rows:
        try:
            fees += Decimal(str(r.fee or 0))
        except Exception:  # noqa: BLE001
            pass
        ann = annotations.get(str(r.id), {})
        try:
            net += Decimal(str(ann.get("trade_pnl_net") or 0))
        except Exception:  # noqa: BLE001
            continue
    return net, fees, len(rows)


@app.get("/orders/rejections")
async def get_order_rejections(
    response: Response,
    limit: int = Query(30, ge=1, le=200),
    session_factory=Depends(_session_factory),
):
    """Recent broker-side order rejections + cancellations.

    This is *execution*-side — i.e. orders the risk engine approved but the
    broker refused (sub-penny price, insufficient BP, closed market,
    unsupported instrument, …) or which were cancelled before filling.
    Complements `/intelligence/signals` (risk-engine rejections).
    """
    response.headers["Cache-Control"] = "no-store, max-age=0"
    async with session_factory() as session:
        q = await session.execute(
            select(OrderLog)
            .where(OrderLog.status.in_(("rejected", "cancelled")))
            .order_by(OrderLog.timestamp.desc())
            .limit(limit)
        )
        rows = list(q.scalars().all())
    return {"rejections": [_order_log_to_dict(r) for r in rows]}


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
async def get_news(
    limit: int = Query(30, ge=1, le=100),
    impactful_only: bool = Query(False, description="Only headlines that influenced recent signal news_score"),
    lookback_hours: int = Query(24, ge=1, le=168),
    session_factory=Depends(_session_factory),
):
    async with session_factory() as session:
        ai_limit = min(2000, max(300, limit * 40))
        if not impactful_only:
            hq = await session.execute(
                select(NewsHeadline)
                .order_by(NewsHeadline.published_at.desc())
                .limit(min(500, limit * 8))
            )
            headlines = [h for h in hq.scalars().all() if is_displayable_news_item(h)][:limit]
            aq = await session.execute(
                select(AIOutputLog)
                .where(AIOutputLog.context_type == "news", AIOutputLog.symbol.isnot(None))
                .order_by(AIOutputLog.timestamp.desc())
                .limit(ai_limit)
            )
            ai_rows = [r for r in aq.scalars().all() if _news_row_matches_logged_symbol(r)]
        else:
            cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
            sq = await session.execute(
                select(SignalLog.symbol.distinct())
                .where(
                    SignalLog.timestamp >= cutoff,
                    SignalLog.symbol.isnot(None),
                    SignalLog.news_score.isnot(None),
                    func.abs(SignalLog.news_score) > 0,
                )
                .limit(ai_limit)
            )
            impacted_symbols = [str(s).strip().upper() for s in sq.scalars().all() if str(s).strip()]
            if not impacted_symbols:
                return {"headlines": [], "ai_scores": []}

            aq = await session.execute(
                select(AIOutputLog)
                .where(
                    AIOutputLog.context_type == "news",
                    AIOutputLog.symbol.in_(impacted_symbols),
                    AIOutputLog.score.isnot(None),
                    func.abs(AIOutputLog.score) > 0,
                    AIOutputLog.timestamp >= cutoff,
                )
                .order_by(AIOutputLog.timestamp.desc())
                .limit(ai_limit)
            )
            ai_rows = [r for r in aq.scalars().all() if _news_row_matches_logged_symbol(r)]
            # Build ticker headlines from AI-linked rows to guarantee "had effect".
            # Keep first seen per (symbol, headline) in timestamp-desc order.
            seen: set[tuple[str, str]] = set()
            headline_rows: list[dict[str, Any]] = []
            for r in ai_rows:
                payload = r.payload if isinstance(r.payload, dict) else {}
                head = str(payload.get("headline") or "").strip()
                if not head:
                    continue
                sym = str(r.symbol or "").strip().upper()
                key = (sym, head.lower())
                if key in seen:
                    continue
                seen.add(key)
                source_name = str(payload.get("provider") or r.source or "ai")
                headline_rows.append(
                    {
                        "title": head,
                        "source": source_name,
                        "published_at": r.timestamp.isoformat() if r.timestamp else None,
                        "url": "",
                        "description": str(r.rationale or "")[:300] or None,
                    }
                )
                if len(headline_rows) >= limit:
                    break
            headlines = []

    best_per_sym = _pick_strongest_news_log_per_symbol(ai_rows)
    ranked = sorted(
        best_per_sym.values(),
        key=lambda r: abs(float(r.score) if r.score is not None else 0.0),
        reverse=True,
    )[:limit]
    ai_scores = [
        {
            "symbol": r.symbol,
            "score": _decimal_str(r.score) if r.score is not None else None,
            "confidence": _decimal_str(r.confidence) if r.confidence is not None else None,
            "event_type": r.event_type,
            "rationale": r.rationale,
            "scored_at": r.timestamp.isoformat() if r.timestamp else None,
        }
        for r in ranked
    ]

    return {
        "headlines": (
            headline_rows
            if impactful_only
            else [
                {
                    "title": h.title,
                    "source": h.source_name,
                    "published_at": h.published_at.isoformat() if h.published_at else None,
                    "url": h.url,
                    "description": h.description,
                }
                for h in headlines
            ]
        ),
        "ai_scores": ai_scores,
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


_UNIVERSE_SNAPSHOT_CACHE: dict[str, Any] = {"at": 0.0, "payload": None}
_UNIVERSE_SNAPSHOT_TTL_SEC: float = 15.0


@app.get("/intelligence/universe")
async def get_intelligence_universe(response: Response):
    """Universe Intelligence snapshot for the dashboard (tiers, funnel, clusters).

    The heavy work here is asking every connected broker for its full
    supported-symbol catalogue. Two optimizations keep the Universe tab
    snappy:

    * broker queries run **in parallel** via ``asyncio.gather`` rather than
      sequentially (so worst-case latency is one slow broker, not the sum
      of all of them); and
    * the assembled payload is cached for a short TTL so concurrent or
      rapidly-repeated requests (tab switches, manual refresh) reuse the
      previous result instead of repaying the catalogue cost.
    """
    from universe.snapshot_service import build_universe_snapshot_dict

    response.headers["Cache-Control"] = "no-store, max-age=0"

    now = _time.monotonic()
    cached = _UNIVERSE_SNAPSHOT_CACHE.get("payload")
    if cached is not None and now - float(_UNIVERSE_SNAPSHOT_CACHE.get("at") or 0.0) < _UNIVERSE_SNAPSHOT_TTL_SEC:
        return cached

    orch = _get_orchestrator()
    bm = getattr(orch, "_broker_manager", None) if orch else None
    broker_total: dict[str, int] = {}
    broker_symbols: dict[str, list[str]] = {}
    if bm is not None and bm.adapters:
        names = list(bm.adapters.keys())

        async def _symbols(name: str) -> tuple[str, list[str]]:
            try:
                syms = await asyncio.wait_for(
                    bm.adapters[name].get_supported_symbols(), timeout=5
                )
                return name, [str(s) for s in (syms or []) if str(s).strip()]
            except Exception:  # noqa: BLE001
                return name, []

        rows = await asyncio.gather(*(_symbols(n) for n in names), return_exceptions=False)
        broker_symbols = {name: syms for name, syms in rows}
        broker_total = {name: len(syms) for name, syms in broker_symbols.items()}

    payload = build_universe_snapshot_dict(
        broker_symbol_totals=broker_total,
        broker_symbols=broker_symbols,
    )
    _UNIVERSE_SNAPSHOT_CACHE["payload"] = payload
    _UNIVERSE_SNAPSHOT_CACHE["at"] = now
    return payload


@app.get("/intelligence/regime")
async def get_intelligence_regime(
    response: Response,
    session_factory=Depends(_session_factory),
):
    """Latest macro regime + top news-scored symbols."""
    response.headers["Cache-Control"] = "no-store, max-age=0"
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
            .limit(1200)
        )
        news_rows = list(news_q.scalars().all())

    best = _pick_strongest_news_log_per_symbol(news_rows)
    # Only show symbols with a real score; 0.0 means no relevant news was found in the best row.
    top_movers = sorted(
        [
            {
                "symbol": s,
                "score": float(r.score) if r.score is not None else 0.0,
                "event_type": r.event_type or "other",
                "rationale": (r.rationale or "")[:200],
                "scored_at": r.timestamp.isoformat() if r.timestamp else None,
            }
            for s, r in best.items()
            if (float(r.score) if r.score is not None else 0.0) != 0.0
        ],
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
    response: Response,
    limit: int = Query(10, ge=1, le=50),
    session_factory=Depends(_session_factory),
):
    """Recent signals annotated with their risk verdict.
    Prefers signals from the last 6h; falls back to latest N if none exist recently.
    Deduplicates by (symbol, strategy, side) so the dashboard shows the newest row per idea.
    """
    response.headers["Cache-Control"] = "no-store, max-age=0"
    from datetime import timedelta
    recent_cutoff = datetime.now(timezone.utc) - timedelta(hours=6)
    fetch_cap = min(500, limit * 25)
    async with session_factory() as session:
        # Fetch recent signals; exclude legacy "max_trades_per_day" rejections
        sig_q = await session.execute(
            select(SignalLog)
            .where(SignalLog.timestamp >= recent_cutoff)
            .order_by(SignalLog.timestamp.desc())
            .limit(fetch_cap)
        )
        sigs_raw = list(sig_q.scalars().all())

        # Collect risk verdicts so we can filter out legacy-only rejections
        legacy_signal_ids: set = set()
        legacy_check = "max_trades_per_day"
        if sigs_raw:
            raw_ids = [s.id for s in sigs_raw]
            rl_q = await session.execute(
                select(RiskLog).where(RiskLog.signal_id.in_(raw_ids))
            )
            for rv in rl_q.scalars().all():
                checks = rv.checks_failed or []
                if checks and all(c == legacy_check for c in checks):
                    legacy_signal_ids.add(rv.signal_id)

        sigs = [s for s in sigs_raw if s.id not in legacy_signal_ids]

        # Absolute fallback: newest N signals from all time (still excluding legacy)
        if not sigs:
            sig_q = await session.execute(
                select(SignalLog).order_by(SignalLog.timestamp.desc()).limit(fetch_cap)
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
            sigs = [s for s in sigs_raw if s.id not in legacy_signal_ids]

        verdicts: dict[str, dict] = {}
        attribution_by_signal: dict[str, list[dict[str, Any]]] = {}
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
            # News attribution: map each signal to nearby AI "news" logs for same symbol.
            sig_symbols = sorted(
                {
                    ss
                    for s in sigs
                    if (ss := _canonical_symbol_for_news_lookup((s.symbol or "").strip())).strip()
                }
            )
            lookup_symbols = _news_lookup_symbols_for_signals(sig_symbols)
            if lookup_symbols:
                min_ts = min((s.timestamp for s in sigs if s.timestamp is not None), default=None)
                max_ts = max((s.timestamp for s in sigs if s.timestamp is not None), default=None)
                if min_ts is not None and max_ts is not None:
                    min_ts_tz = min_ts if min_ts.tzinfo else min_ts.replace(tzinfo=timezone.utc)
                    max_ts_tz = max_ts if max_ts.tzinfo else max_ts.replace(tzinfo=timezone.utc)
                    # Cover the widest per-row symmetric window ``_signal_news_attribution``
                    # uses (+ batch skew) around all signals fetched in one request.
                    min_cutoff = min_ts_tz - timedelta(hours=72)
                    max_cutoff = max_ts_tz + timedelta(hours=72)
                    ai_q = await session.execute(
                        select(AIOutputLog)
                        .where(
                            AIOutputLog.context_type == "news",
                            AIOutputLog.symbol.in_(lookup_symbols),
                            AIOutputLog.timestamp >= min_cutoff,
                            AIOutputLog.timestamp <= max_cutoff,
                        )
                        .order_by(AIOutputLog.timestamp.desc())
                        .limit(3000)
                    )
                    ai_rows = list(ai_q.scalars().all())
                    by_sym: dict[str, list[Any]] = {}
                    market_rows: list[Any] = []
                    for r in ai_rows:
                        sym = (getattr(r, "symbol", None) or "").strip().upper()
                        if not sym:
                            continue
                        by_sym.setdefault(sym, []).append(r)
                        if _is_market_wide_news_row(r):
                            market_rows.append(r)
                    for s in sigs:
                        sig_sym = _canonical_symbol_for_news_lookup((s.symbol or "").strip())
                        if not sig_sym:
                            continue
                        direct_rows = _candidate_news_rows_for_signal(sig_sym, by_sym.get(sig_sym, []))
                        direct = _signal_news_attribution(s, direct_rows, max_items=2)
                        if direct:
                            for d in direct:
                                d["match_mode"] = "direct"
                            attribution_by_signal[s.id] = direct
                            continue
                        aliased_rows: list[Any] = []
                        for a in _alias_symbols_for_signal(sig_sym):
                            aliased_rows.extend(by_sym.get(a, []))
                        aliased_rows = _candidate_news_rows_for_signal(sig_sym, aliased_rows)
                        alias_attr = _signal_news_attribution(s, aliased_rows, max_items=2)
                        if alias_attr:
                            for d in alias_attr:
                                d["match_mode"] = "alias"
                            attribution_by_signal[s.id] = alias_attr
                            continue
                        market_attr = _signal_news_attribution(s, market_rows, max_items=2)
                        for d in market_attr:
                            d["match_mode"] = "market"
                        attribution_by_signal[s.id] = market_attr

    # Dashboard: one row per (symbol, strategy, side) — keep the newest only. The runner may log
    # the same idea every few minutes; showing six identical lines is correct historically but noisy.
    sigs_ordered = list(sigs)
    seen_keys: set[tuple[str, str, str]] = set()
    sigs_deduped: list[Any] = []
    for s in sigs_ordered:
        key = (
            (s.symbol or "").strip().upper(),
            (s.strategy or "").strip().lower(),
            (s.side or "").strip().lower(),
        )
        if key in seen_keys:
            continue
        seen_keys.add(key)
        sigs_deduped.append(s)
        if len(sigs_deduped) >= limit:
            break

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
                "ai_news_score": _metadata_float(s.metadata_, "ai_news_score"),
                "accumulator_score": _metadata_float(s.metadata_, "accumulator_score"),
                "quality_score": (float(s.metadata_["trade_quality_score"]) if s.metadata_ and "trade_quality_score" in s.metadata_ else None),
                "volume_z": (float(s.metadata_["volume_z_score"]) if s.metadata_ and "volume_z_score" in s.metadata_ else None),
                "verdict": verdicts.get(s.id, {}).get("verdict", "unknown"),
                "risk_reason": verdicts.get(s.id, {}).get("reason", ""),
                "checks_failed": verdicts.get(s.id, {}).get("checks_failed", []),
                "news_attribution": attribution_by_signal.get(s.id, []),
                "news_impact_source": _signal_news_impact_source(s, attribution_by_signal.get(s.id, [])),
            }
            for s in sigs_deduped
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
    """Sum net-liquidation for brokers in coverage (D031).

    Delegates to :func:`system.portfolio_equity.live_portfolio_value`, the
    canonical helper used by the trading loop. It prefers the ``BASE`` currency
    row (IBKR NetLiquidation) and only includes adapters in
    ``BrokerManager.report.included_names`` and not in ``RiskEngine.disabled_brokers``.
    """
    orch = _get_orchestrator()
    if orch is None:
        return Decimal(0)
    bm = getattr(orch, "_broker_manager", None)
    if bm is None:
        return Decimal(0)
    return await live_portfolio_value(bm)


async def _live_portfolio_nav_status() -> dict[str, Any]:
    orch = _get_orchestrator()
    if orch is None:
        return {"complete": False, "included": [], "missing": []}
    bm = getattr(orch, "_broker_manager", None)
    if bm is None:
        return {"complete": False, "included": [], "missing": []}
    snap = await live_portfolio_snapshot(bm)
    return {
        "complete": bool(snap.complete),
        "included": list(snap.included),
        "missing": list(snap.missing),
    }


def _nav_status_from_snapshot(snap: Any) -> dict[str, Any]:
    cov = _current_broker_coverage()
    coverage_full = bool(cov.get("full")) if isinstance(cov, dict) else bool(getattr(snap, "complete", False))
    status = {
        "complete": bool(getattr(snap, "complete", False)),
        "included": list(getattr(snap, "included", ()) or ()),
        "missing": list(getattr(snap, "missing", ()) or ()),
        "coverage_full": coverage_full,
    }
    if isinstance(cov, dict):
        status["configured"] = list(cov.get("configured") or [])
        status["excluded"] = list(cov.get("excluded") or [])
    return status


def _partial_coverage_period_rollup(period_agg: dict[str, Any]) -> dict[str, Any]:
    """Keep period metadata but avoid full-book P&L on a partial broker NAV."""
    out = dict(period_agg)
    out["realised"] = "0"
    out["unrealised"] = "0"
    out["fees"] = "0"
    out["trades"] = 0
    out["partial_coverage"] = True
    return out


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
async def get_pnl(
    session_factory=Depends(_session_factory),
    bus: CommandBus = Depends(_command_bus),
):
    today_d = date.today()
    today = today_d.isoformat()
    async with session_factory() as session:
        today_q = await session.execute(select(DailyPnL).where(DailyPnL.date == today).limit(1))
        today_row = today_q.scalars().first()
        agg_q = await session.execute(
            select(
                func.coalesce(func.sum(DailyPnL.realised_pnl), 0).label("realised"),
                func.coalesce(func.sum(DailyPnL.total_fees), 0).label("fees"),
                func.coalesce(func.sum(DailyPnL.trade_count), 0).label("trades"),
            )
        )
        agg = tuple(agg_q.one())
        latest_unrealised_q = await session.execute(
            select(DailyPnL.unrealised_pnl).order_by(DailyPnL.date.desc()).limit(1)
        )
        latest_unrealised_db = Decimal(str(latest_unrealised_q.scalar_one_or_none() or 0))
        ws, we = week_to_date_range(today_d)
        ms, me = month_to_date_range(today_d)
        week_agg = await aggregate_daily_pnl_range(session, ws, we)
        month_agg = await aggregate_daily_pnl_range(session, ms, me)
        # SINGLE SOURCE OF TRUTH. Headline realised/fees/trades come from the
        # canonical persisted ``daily_pnl`` ledger (computed every loop cycle
        # by ``_compute_today_realised_pnl`` — a flat-start per-day FIFO
        # replay). The previous full-history order replay
        # (``_filled_order_net_pnl_for_period`` over thousands of orders,
        # seeded from PositionLog) accumulated FIFO/seed drift across
        # days+restarts and produced a wildly wrong "all-time" (e.g. −$32k
        # while NAV was flat) — and made this endpoint slow/time out. It is
        # retained only for per-order annotations (/orders, realised-curve).
        today_net_realised = (
            Decimal(str(today_row.realised_pnl or 0)) if today_row else Decimal("0")
        )
        today_order_fees = (
            Decimal(str(today_row.total_fees or 0)) if today_row else Decimal("0")
        )
        today_order_trades = int(today_row.trade_count or 0) if today_row else 0
        # All-time = sum of every persisted daily row (``agg`` above is the
        # unfiltered DailyPnL sum). Drift-free and internally consistent
        # with today/week/month, which now all read the same ledger.
        ytd_net_realised = Decimal(str(agg[0] or 0))
        ytd_order_fees = Decimal(str(agg[1] or 0))
        ytd_order_trades = int(agg[2] or 0)
        pv_q = await session.execute(select(DailyPnL.portfolio_value).order_by(DailyPnL.date.asc()).limit(400))
        pv_vals: list[Decimal] = []
        for x in pv_q.scalars().all():
            if x is None:
                continue
            try:
                pv_vals.append(Decimal(str(x)))
            except Exception:  # noqa: BLE001
                continue
        max_dd = equity_max_drawdown_pct(pv_vals)
        win_rate = await win_rate_from_daily_rows(session)

    mtm_unrealised = await _compute_live_unrealised_mtm(session_factory)
    db_today_unrealised = Decimal(str(today_row.unrealised_pnl if today_row else 0))
    today_unrealised = mtm_unrealised if mtm_unrealised != 0 else db_today_unrealised

    db_value = today_row.portfolio_value if today_row and today_row.portfolio_value else Decimal(0)
    # If today's row has not been written yet (quiet morning, just-restarted
    # backend, or heartbeat not fired yet), fall back to the most recent
    # persisted NAV. Prevents the UI from dropping back to ``PORTFOLIO_VALUE``
    # (100k default) when brokers are slow to report balances post-restart.
    last_persisted_value = pv_vals[-1] if pv_vals else Decimal(0)
    orch = _get_orchestrator()
    bm = getattr(orch, "_broker_manager", None) if orch is not None else None
    live_snap = await live_portfolio_snapshot(bm) if bm is not None else None
    nav_status = _nav_status_from_snapshot(live_snap) if live_snap is not None else {"complete": False, "included": [], "missing": []}
    live_value = live_snap.value if live_snap is not None else Decimal(0)
    coverage_full = bool(nav_status.get("coverage_full", nav_status.get("complete", False)))
    configured_nav = _configured_paper_nav()
    # When brokers report a positive live sum, that figure wins — do not `max` with
    # `daily_pnl` or older persisted rows that were written while an excluded/buggy
    # broker was still in the pre-allowlist live sum (D031). The DB is used only
    # when `live_value` is still zero (e.g. slow first snapshot post-restart).
    live_value_used = live_value > 0
    if live_value_used:
        display_value = live_value
    else:
        display_value = max(live_value, db_value, last_persisted_value, configured_nav)
    if APP_ENV != "live" and not live_value_used:
        display_value = max(Decimal(0), display_value + today_unrealised)

    orch = _get_orchestrator()
    cap_pct = 1.0
    if orch is not None:
        try:
            cap_pct = float(getattr(orch, "capital_pct", 1.0))
        except (TypeError, ValueError):
            cap_pct = 1.0
    try:
        cap_raw = await bus.get_state(CAPITAL_ALLOCATION_STATE_KEY, None)
        if isinstance(cap_raw, dict) and "pct" in cap_raw:
            cap_pct = float(cap_raw.get("pct", cap_pct))
    except Exception:  # noqa: BLE001
        pass
    cap_pct = max(0.0, min(1.0, cap_pct))
    tradable_value = display_value * Decimal(str(cap_pct))

    # Merge live MTM for today into week/month, even when today's ``DailyPnL`` row
    # does not exist yet (``db_today`` = 0) — otherwise W/M stay 0 while "today" moves.
    db_today_u = Decimal(str(today_row.unrealised_pnl or 0)) if today_row else Decimal(0)
    for period_agg in (week_agg, month_agg):
        u = Decimal(str(period_agg["unrealised"]))
        period_agg["unrealised"] = str(
            merge_live_today_unrealised_into_period(
                u,
                db_today_unrealised=db_today_u,
                live_today_unrealised=today_unrealised,
            )
        )
    # week_agg / month_agg keep their ``aggregate_daily_pnl_range`` values
    # (canonical daily_pnl sums) — no longer overwritten with the drift-prone
    # full-history order replay, so every period reconciles with each other
    # and with the loop's persisted realised.
    if not coverage_full:
        # DailyPnL/history rows are whole-book aggregates. When the live NAV is
        # intentionally partial (for example IBKR is offline and excluded), do
        # not compare that partial denominator with full-book historical P&L.
        week_agg = _partial_coverage_period_rollup(week_agg)
        month_agg = _partial_coverage_period_rollup(month_agg)
        max_dd = None
    all_time_unrealised = (
        today_unrealised
        if coverage_full and today_unrealised != 0
        else (latest_unrealised_db if coverage_full else Decimal(0))
    )

    return {
        "today": {
            "realised": _decimal_str(today_net_realised if coverage_full else 0),
            "unrealised": _decimal_str(today_unrealised),
            "fees": _decimal_str(today_order_fees if coverage_full else 0),
            "trades": int(today_order_trades if coverage_full else 0),
            "portfolio_value": _decimal_str(display_value),
            "tradable_capital": _decimal_str(tradable_value),
            "capital_allocation_pct": cap_pct,
            "nav_status": nav_status,
        },
        "all_time": {
            "realised": _decimal_str(0 if not coverage_full else ytd_net_realised),
            "unrealised": _decimal_str(all_time_unrealised),
            "fees": _decimal_str(0 if not coverage_full else ytd_order_fees),
            "trades": int(ytd_order_trades or 0) if coverage_full else 0,
            "partial_coverage": not coverage_full,
        },
        "week": week_agg,
        "month": month_agg,
        "metrics": {
            "win_rate_days": win_rate,
            "max_drawdown_pct": max_dd,
        },
    }


@app.get("/dashboard/wave13")
async def get_dashboard_wave13(bus: CommandBus = Depends(_command_bus)):
    """
    Wave 13 — structured observability payload.

    Aggregates the opportunity funnel (``system/funnel_telemetry.py``),
    strategy coverage (which YAML gates are flipped), model health
    (registered models + feature freshness), portfolio intelligence
    (latest allocation snapshot + Wave 8 overlay diagnostics), and
    execution intelligence (Wave 9 cost-gate counters).
    """
    from api.wave13_dashboard import build_wave13_payload

    raw_snapshot = await bus.get_state(DASHBOARD_SNAPSHOT_KEY, None)
    snap_for_payload = raw_snapshot if isinstance(raw_snapshot, dict) else None
    return build_wave13_payload(
        snapshot=snap_for_payload,
    )


@app.get("/dashboard/snapshot")
async def get_dashboard_snapshot(
    bus: CommandBus = Depends(_command_bus),
):
    """Latest allocator + accumulator snapshot from ``ControlState`` (trading loop)."""
    raw = await bus.get_state(DASHBOARD_SNAPSHOT_KEY, None)
    if not isinstance(raw, dict):
        return {}
    out = dict(raw)
    session_factory = getattr(app.state, "db_session_factory", None)
    if APP_ENV != "live":
        if session_factory is not None:
            pos_payload = await get_positions(limit=500, session_factory=session_factory)
            position_rows = list(pos_payload.get("positions") or []) if isinstance(pos_payload, dict) else []
            position_source = str(pos_payload.get("source", "position_log")) if isinstance(pos_payload, dict) else "position_log"
        else:
            position_rows = []
            position_source = "position_log"
    else:
        position_rows = await _live_broker_positions(500)
        position_source = "live_broker"
    nav = await _live_portfolio_value()
    gross = Decimal(0)
    net = Decimal(0)
    cash_deployed = Decimal(0)
    total_unrealised = Decimal(0)
    sample: list[dict[str, Any]] = []
    held_edges: list[dict[str, Any]] = []
    for p in position_rows:
        try:
            qty = Decimal(str(p.get("quantity", "0") or "0"))
            px = Decimal(str(p.get("current_price", "0") or "0"))
            pnl = Decimal(str(p.get("unrealised_pnl", "0") or "0"))
        except Exception:  # noqa: BLE001
            continue
        mv = qty * px
        asset_class = str(p.get("asset_class") or "")
        symbol = str(p.get("symbol") or "")
        gross += abs(mv)
        net += mv
        abs_mv = abs(mv)
        cash_deployed += abs_mv * cash_factor_for_asset_class(asset_class, symbol=symbol)
        total_unrealised += pnl
        sample.append(
            {
                "symbol": symbol,
                "asset_class": asset_class,
                "side": "short" if qty < 0 else "long",
                "market_value": _decimal_str(abs_mv),
                "unrealised_pnl": _decimal_str(pnl),
                "broker": p.get("broker"),
                "tags": [],
            }
        )
        held_edges.append(
            {
                "symbol": symbol,
                "notional": _decimal_str(abs_mv),
                "expected_remaining_edge": "0",
                "strategy_name": "held_position",
                "broker": p.get("broker"),
            }
        )
    portfolio = dict(out.get("portfolio") or {})
    if nav > 0:
        portfolio["nav"] = _decimal_str(nav)
    portfolio["gross_exposure"] = _decimal_str(gross)
    portfolio["net_exposure"] = _decimal_str(net)
    portfolio["cash_deployed"] = _decimal_str(cash_deployed)
    portfolio["cash_deployed_pct"] = _decimal_str((cash_deployed / nav) if nav > 0 else Decimal("0"))
    portfolio["positions_sample"] = sample[:24]
    portfolio["weakest_by_hold_score"] = []
    portfolio["highest_exit_pressure"] = []
    portfolio["unrealised_pnl"] = _decimal_str(total_unrealised)
    portfolio["source"] = position_source
    out["portfolio"] = portfolio
    ge = dict(out.get("global_edge") or {})
    ge["held_edges"] = held_edges
    out["global_edge"] = ge
    return out


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


@app.get("/pnl/realised-curve")
async def get_pnl_realised_curve(
    response: Response,
    days: int = Query(400, ge=1, le=1100),
    session_factory=Depends(_session_factory),
):
    """Daily *realised* P&L series from the canonical ``daily_pnl`` ledger.

    SINGLE SOURCE OF TRUTH. This reads the same persisted per-day ledger
    as the headline ``/pnl`` numbers, so the cumulative graph and the big
    number can never disagree. It previously used a full-history
    order-replay (``_order_realised_pnl_annotations``) that accumulated
    FIFO/seed drift across days+restarts and rendered a phantom dip
    (e.g. the −$32k that never happened). The frontend turns this into a
    cumulative curve and re-bases it to zero at the start of whichever
    window (Historical / YTD / Month / Week / Today) the operator selects.
    """
    response.headers["Cache-Control"] = "no-store, max-age=0"
    end_day = datetime.now(timezone.utc).date()
    start_day = end_day - timedelta(days=days - 1)
    start_s = start_day.isoformat()
    end_s = end_day.isoformat()

    async with session_factory() as session:
        q = await session.execute(
            select(DailyPnL)
            .where(DailyPnL.date >= start_s, DailyPnL.date <= end_s)
            .order_by(DailyPnL.date.asc())
        )
        drows = list(q.scalars().all())

    by_day: dict[str, Decimal] = {}
    trades_by_day: dict[str, int] = {}
    for dr in drows:
        try:
            by_day[str(dr.date)] = Decimal(str(dr.realised_pnl or 0))
        except Exception:  # noqa: BLE001
            by_day[str(dr.date)] = Decimal("0")
        trades_by_day[str(dr.date)] = int(dr.trade_count or 0)

    series: list[dict[str, Any]] = []
    cumulative = Decimal("0")
    cur = start_day
    while cur <= end_day:
        key = cur.isoformat()
        realised = by_day.get(key, Decimal("0"))
        cumulative += realised
        series.append(
            {
                "date": key,
                "realised": _decimal_str(realised),
                "cumulative": _decimal_str(cumulative),
                "trades": trades_by_day.get(key, 0),
            }
        )
        cur += timedelta(days=1)

    return {
        "as_of": datetime.now(timezone.utc).isoformat(),
        "start": start_day.isoformat(),
        "end": end_day.isoformat(),
        "series": series,
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
    payload: dict[str, Any] | None = Body(default=None),
):
    """Halt trading: global kill + cancel all (default), or disable specific brokers only when ``brokers`` is set."""
    body = dict(payload or {})
    bus = getattr(app.state, "command_bus", None)
    if bus is not None:
        cmd_id = await bus.enqueue("kill", body, source="api")
        return {"kill_switch": True, "command_id": cmd_id, "message": "Kill command enqueued"}
    # fallback for single-process mode
    risk_engine = get_risk_engine()
    execution_engine = get_execution_engine()
    if risk_engine is None:
        raise HTTPException(status_code=503, detail="Risk engine not registered")
    raw_brokers = body.get("brokers")
    if isinstance(raw_brokers, list) and raw_brokers:
        for b in raw_brokers:
            risk_engine.disable_broker(str(b))
    else:
        risk_engine.kill()
        if execution_engine is not None:
            await execution_engine.cancel_all()
    return {"kill_switch": True, "message": "Kill applied"}


@app.post("/kill/reset")
async def reset_kill_switch(
    _: None = Depends(_require_mutation_token),
    payload: dict[str, Any] | None = Body(default=None),
):
    """Re-enable trading: full reset (default) or re-enable specific brokers."""
    body = dict(payload or {})
    bus = getattr(app.state, "command_bus", None)
    if bus is not None:
        cmd_id = await bus.enqueue("reset_kill", body, source="api")
        return {"kill_switch": False, "command_id": cmd_id, "message": "Reset command enqueued"}
    risk_engine = get_risk_engine()
    if risk_engine is None:
        raise HTTPException(status_code=503, detail="Risk engine not registered")
    raw_brokers = body.get("brokers")
    if isinstance(raw_brokers, list) and raw_brokers:
        for b in raw_brokers:
            risk_engine.enable_broker(str(b))
    else:
        risk_engine.reset_kill()
    return {"kill_switch": False, "message": "Kill reset applied"}


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
async def system_status(
    bus: CommandBus = Depends(_command_bus),
    session_factory=Depends(_optional_session_factory),
):
    """Full system status including state, brokers, infrastructure, data providers."""
    from data.ingest_telemetry import build_news_data_provider_status, build_news_data_provider_status_env_only

    async def _merge_providers(data: dict) -> dict:
        npp: list[dict] = build_news_data_provider_status_env_only()
        if session_factory is not None:
            try:
                npp = await build_news_data_provider_status(session_factory)
            except Exception:  # noqa: BLE001
                npp = build_news_data_provider_status_env_only()
        if not npp:
            npp = build_news_data_provider_status_env_only()
        return {**data, "news_data_providers": npp}

    orch = _get_orchestrator()
    if orch is None:
        return await _merge_providers(
            {
                "state": "off",
                "paper_mode": APP_ENV != "live",
                "active_brokers": [],
                "brokers": {},
                "infrastructure": {},
                "trading": {"running": False},
                "errors": ["Orchestrator not initialized"],
                "pipeline_running": False,
            }
        )
    out = orch.status()
    try:
        cap_raw = await bus.get_state(CAPITAL_ALLOCATION_STATE_KEY, None)
        if isinstance(cap_raw, dict):
            cap_raw = cap_raw.get("pct")
        if cap_raw is not None and str(out.get("state", "")).lower() != "running":
            out["capital_pct"] = max(0.0, min(1.0, float(cap_raw)))
        dash_raw = await bus.get_state(DASHBOARD_SNAPSHOT_KEY, None)
        if isinstance(dash_raw, dict):
            ua = dash_raw.get("updated_at")
            if isinstance(ua, str) and ua.strip():
                tr = out.get("trading")
                if isinstance(tr, dict):
                    out["trading"] = {**tr, "snapshot_published_at": ua.strip()}
        rt_hb = await bus.get_state("runtime.heartbeat", None)
        if isinstance(rt_hb, dict):
            ai_st = rt_hb.get("ai")
            if isinstance(ai_st, dict):
                tr2 = out.get("trading")
                if isinstance(tr2, dict):
                    out["trading"] = {**tr2, "ai": ai_st}
    except Exception:  # noqa: BLE001
        pass
    return await _merge_providers(out)


@app.get("/diagnostics/strategy-candidates")
async def diagnostics_strategy_candidates(
    since_hours: float = Query(24, ge=0.5, le=168),
    session_factory=Depends(_session_factory),
):
    """Strategy Mix metrics from :class:`~storage.models.StrategyCandidateLog` (D033)."""
    if session_factory is None:
        return {"since_hours": since_hours, "strategies": [], "error": "no_database"}
    from system.strategy_candidate_log import fetch_strategy_mix_diagnostics

    return await fetch_strategy_mix_diagnostics(session_factory, since_hours=since_hours)


@app.get("/diagnostics/accounting")
async def diagnostics_accounting(
    session_factory=Depends(_session_factory),
):
    """Accounting health checks for paper/live book consistency."""
    if session_factory is None:
        return {"ok": False, "error": "no_database", "warnings": ["database_unavailable"]}

    warnings: list[str] = []
    errors: list[str] = []
    async with session_factory() as session:
        latest_q = await session.execute(
            text(
                """
                WITH latest AS (
                  SELECT DISTINCT ON (broker, symbol)
                    broker, symbol, quantity, current_price, avg_entry_price,
                    unrealised_pnl, timestamp
                  FROM positions
                  ORDER BY broker, symbol, timestamp DESC, id DESC
                )
                SELECT broker, symbol, quantity, current_price, avg_entry_price,
                       unrealised_pnl, timestamp
                FROM latest
                ORDER BY broker, symbol
                """
            )
        )
        latest_rows = list(latest_q.mappings().all())

        shock_q = await session.execute(
            text(
                """
                WITH ordered AS (
                  SELECT
                    broker,
                    symbol,
                    quantity,
                    timestamp,
                    lag(quantity) OVER (
                      PARTITION BY broker, symbol
                      ORDER BY timestamp, id
                    ) AS prev_quantity,
                    lag(timestamp) OVER (
                      PARTITION BY broker, symbol
                      ORDER BY timestamp, id
                    ) AS prev_timestamp
                  FROM positions
                  WHERE timestamp >= now() - interval '48 hours'
                )
                SELECT broker, symbol, quantity, timestamp,
                       prev_quantity, prev_timestamp
                FROM ordered
                WHERE abs(quantity) <= 0.00000001
                  AND abs(coalesce(prev_quantity, 0)) > 0.00000001
                ORDER BY timestamp DESC, broker, symbol
                LIMIT 100
                """
            )
        )
        shocks = list(shock_q.mappings().all())

        today_q = await session.execute(
            select(DailyPnL).where(DailyPnL.date == date.today().isoformat())
        )
        today = today_q.scalars().first()
        fee_q = await session.execute(
            select(func.coalesce(func.sum(OrderLog.fee), 0))
            .where(
                func.date(OrderLog.timestamp) == date.today(),
                OrderLog.status == "filled",
            )
        )
        order_fee_total = Decimal(str(fee_q.scalar_one() or 0))

    open_rows = [r for r in latest_rows if abs(Decimal(str(r["quantity"] or 0))) > Decimal("0.00000001")]
    open_by_broker: dict[str, int] = {}
    exposure_by_broker: dict[str, Decimal] = {}
    current_unrealised = Decimal(0)
    for r in open_rows:
        broker = str(r["broker"] or "").strip().lower()
        open_by_broker[broker] = open_by_broker.get(broker, 0) + 1
        qty = Decimal(str(r["quantity"] or 0))
        px = Decimal(str(r["current_price"] or 0))
        pnl = Decimal(str(r["unrealised_pnl"] or 0))
        exposure_by_broker[broker] = exposure_by_broker.get(broker, Decimal(0)) + abs(qty * px)
        current_unrealised += pnl

    if shocks:
        warnings.append("recent_position_tombstone_shock")

    persisted_unrealised = Decimal(str(today.unrealised_pnl or 0)) if today is not None else Decimal(0)
    pnl_delta = current_unrealised - persisted_unrealised
    if abs(pnl_delta) > Decimal("1"):
        errors.append("daily_pnl_unrealised_differs_from_open_book")
    persisted_fees = Decimal(str(today.total_fees or 0)) if today is not None else Decimal(0)
    fee_delta = order_fee_total - persisted_fees
    if abs(fee_delta) > Decimal("1"):
        warnings.append("daily_pnl_fees_differ_from_filled_orders")

    return {
        "ok": not errors,
        "paper_mode": APP_ENV != "live",
        "warnings": warnings,
        "errors": errors,
        "open_positions": {
            "count": len(open_rows),
            "by_broker": open_by_broker,
            "exposure_by_broker": {k: _decimal_str(v) for k, v in exposure_by_broker.items()},
            "unrealised_pnl": _decimal_str(current_unrealised),
        },
        "daily_pnl": {
            "date": date.today().isoformat(),
            "unrealised_pnl": _decimal_str(persisted_unrealised),
            "open_book_delta": _decimal_str(pnl_delta),
            "fees": _decimal_str(persisted_fees),
            "filled_order_fees": _decimal_str(order_fee_total),
            "fee_delta": _decimal_str(fee_delta),
        },
        "recent_tombstone_shocks": [
            {
                "broker": str(r["broker"]),
                "symbol": str(r["symbol"]),
                "timestamp": r["timestamp"].isoformat() if r["timestamp"] else None,
                "previous_quantity": _decimal_str(Decimal(str(r["prev_quantity"] or 0))),
                "previous_timestamp": r["prev_timestamp"].isoformat() if r["prev_timestamp"] else None,
            }
            for r in shocks
        ],
    }


@app.get("/diagnostics/routing-quality")
async def diagnostics_routing_quality(bus: CommandBus = Depends(_command_bus)):
    """Return persisted broker-symbol routing quality map/history."""
    state = await bus.get_state("routing.quality.state", {}) or {}
    runtime = await bus.get_state("runtime.heartbeat", {}) or {}
    rt_extra = runtime.get("extra") if isinstance(runtime, dict) else None
    rt_rq = rt_extra.get("routing_quality") if isinstance(rt_extra, dict) else None
    if not isinstance(state, dict):
        state = {}
    return {
        "updated_at": state.get("updated_at"),
        "quality_map": state.get("quality_map", {}),
        "quality_stats": state.get("quality_stats", {}),
        "history": state.get("history", {}),
        "broker_comparison": state.get("broker_comparison", []),
        "exec_metrics": state.get("exec_metrics", {}),
        "runtime_summary": rt_rq if isinstance(rt_rq, dict) else {},
    }


@app.post("/system/start")
async def system_start(_: None = Depends(_require_mutation_token)):
    """Start the entire trading system (one-button ON)."""
    orch = _get_orchestrator()
    if orch is None:
        raise HTTPException(status_code=503, detail="Orchestrator not available")
    result = await orch.start()
    return result


@app.post("/system/stop")
async def system_stop(_: None = Depends(_require_mutation_token)):
    """Stop the entire trading system (one-button OFF)."""
    orch = _get_orchestrator()
    if orch is None:
        raise HTTPException(status_code=503, detail="Orchestrator not available")
    result = await orch.stop()
    return result


@app.put("/system/capital-allocation")
async def set_capital_allocation(
    body: dict,
    _: None = Depends(_require_mutation_token),
):
    """Set the fraction of total equity used for order sizing and sleeve risk limits (0.0–1.0)."""
    orch = _get_orchestrator()
    if orch is None:
        raise HTTPException(status_code=503, detail="Orchestrator not available")
    pct = float(body.get("pct", 1.0))
    if not (0.0 <= pct <= 1.0):
        raise HTTPException(status_code=400, detail="pct must be between 0 and 1")
    bus = getattr(app.state, "command_bus", None)
    if bus is not None:
        await bus.set_state(
            CAPITAL_ALLOCATION_STATE_KEY,
            {
                "pct": pct,
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "source": "api",
            },
        )
    orch.set_capital_pct(pct)
    return {"capital_pct": orch.capital_pct, "persisted": bus is not None}


@app.get("/system/capital-allocation")
async def get_capital_allocation():
    """Get the current capital allocation fraction."""
    orch = _get_orchestrator()
    bus = getattr(app.state, "command_bus", None)
    if bus is not None and (
        orch is None or str(getattr(orch, "state", "")).lower().endswith("off")
    ):
        raw = await bus.get_state(CAPITAL_ALLOCATION_STATE_KEY, None)
        if isinstance(raw, dict):
            try:
                return {"capital_pct": max(0.0, min(1.0, float(raw.get("pct", 1.0))))}
            except (TypeError, ValueError):
                pass
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
            dash_hint = None
            if bus is not None:
                dash_raw = await bus.get_state(DASHBOARD_SNAPSHOT_KEY, None)
                if isinstance(dash_raw, dict):
                    dash_hint = {
                        "updated_at": dash_raw.get("updated_at"),
                        "fingerprint": dash_raw.get("fingerprint"),
                        "path": dash_raw.get("path"),
                        "loop_iteration": dash_raw.get("loop_iteration"),
                    }
            await ws.send_json(
                {
                    "type": "tick",
                    "payload": {
                        "status": status,
                        "system": sys_status,
                        "events": events,
                        "dashboard": dash_hint,
                    },
                    "ts": datetime.now(timezone.utc).isoformat(),
                }
            )
            await asyncio.sleep(2)
    except WebSocketDisconnect:
        return


@app.post("/admin/cancel_pending_orders")
async def admin_cancel_pending_orders(
    broker: str | None = Query(None, description="Optional broker filter (e.g. ibkr, alpaca)"),
    session_factory=Depends(_session_factory),
    _: None = Depends(_require_mutation_token),
):
    """Cancel every non-terminal order across connected brokers.

    Use this after shipping execution changes (e.g. marketable-limit pricing)
    to clear the pre-change backlog of stuck limit orders. Safe to run at any
    time — only affects orders with status ``pending`` / ``open`` /
    ``partially_filled``. Returns a per-broker cancellation summary.
    """
    execution_engine = get_execution_engine()
    brokers_map: dict[str, Any] = (
        dict(getattr(execution_engine, "_brokers", {})) if execution_engine is not None else {}
    )
    if broker:
        b = broker.strip().lower()
        brokers_map = {k: v for k, v in brokers_map.items() if k.lower() == b}

    summary: dict[str, dict[str, int]] = {}
    for bname, badapter in brokers_map.items():
        cancelled = 0
        failed = 0
        try:
            open_orders = await badapter.get_open_orders()
        except Exception as exc:
            summary[bname] = {"cancelled": 0, "failed": 0, "error": str(exc)[:200]}  # type: ignore[dict-item]
            continue
        for o in open_orders:
            boid = getattr(o, "broker_order_id", None)
            if not boid:
                continue
            try:
                ok = await badapter.cancel_order(boid)
                if ok:
                    cancelled += 1
                else:
                    failed += 1
            except Exception:
                failed += 1
        summary[bname] = {"cancelled": cancelled, "failed": failed}

    # Reconcile the DB so the next iteration's dedup-lookup sees a clean
    # slate. Any straggler we couldn't cancel at the broker will be picked up
    # by the usual order-tracking code on the next sync.
    from sqlalchemy import update
    db_updated = 0
    try:
        async with session_factory() as session:
            stmt = (
                update(OrderLog)
                .where(OrderLog.status.in_(("pending", "open", "partially_filled")))
            )
            if broker:
                stmt = stmt.where(OrderLog.broker == broker.strip().lower())
            stmt = stmt.values(status="cancelled")
            result = await session.execute(stmt)
            await session.commit()
            db_updated = int(result.rowcount or 0)
    except Exception as exc:
        return {
            "cancelled_by_broker": summary,
            "db_updated": 0,
            "db_error": str(exc)[:200],
        }

    return {
        "cancelled_by_broker": summary,
        "db_updated": db_updated,
    }


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
async def set_system_mode(
    body: _ModeBody,
    _: None = Depends(_require_mutation_token),
):
    """Setting mode manually is no longer allowed.

    From Phase 0 of the adaptive-mode refactor, mode is **derived state**
    computed every trading-loop iteration from market signals (drawdown,
    cross-section vol, signal density, news emergencies). The operator
    cannot override it — the dashboard pills show the computed value as a
    read-only indicator. See ``system/adaptive_mode.py`` for the classifier.

    The endpoint stays around so old UIs / scripts get a clear 403 with an
    explanation instead of a silent 404. ``_ModeBody`` is read for shape
    validation only.
    """
    _ = body.mode  # validate shape without using it
    raise HTTPException(
        status_code=403,
        detail=(
            "Mode is auto-derived from market state and cannot be set manually. "
            "See GET /system/mode for the current classifier output."
        ),
    )


# ---------------------------------------------------------------------------
# Serve the React UI from ui/dist (must be LAST so API routes take priority)
# ---------------------------------------------------------------------------
_UI_DIR = Path(__file__).resolve().parent.parent / "ui" / "dist"
# Avoid stale UI in browsers that cache aggressively. New builds change chunk
# hashes, but during rapid local iteration the operator should never have to
# wonder whether 127.0.0.1:8000 is serving yesterday's control surface.
_UI_INDEX_HEADERS = {"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache"}
_UI_ASSET_HEADERS = {"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache"}
if _UI_DIR.is_dir():
    _index_html = _UI_DIR / "index.html"

    @app.get("/{full_path:path}", include_in_schema=False)
    async def _spa_fallback(full_path: str):  # noqa: ARG001
        """Serve index.html for any path not matched by the API (SPA routing)."""
        from starlette.responses import FileResponse

        if (_UI_DIR / full_path).is_file():
            p = _UI_DIR / full_path
            if p.name == "index.html":
                return FileResponse(p, headers=_UI_INDEX_HEADERS)
            return FileResponse(p, headers=_UI_ASSET_HEADERS)
        if _index_html.is_file():
            return FileResponse(_index_html, headers=_UI_INDEX_HEADERS)
        raise HTTPException(404)
