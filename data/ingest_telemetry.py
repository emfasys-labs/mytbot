"""
Persist and read last-success timestamps for external news & macro data providers.

Used by the dashboard "News & data providers" card (not per-article ``source_name``).
Keys live in ``ControlState`` under ``data.ingest.telemetry`` as a JSON object:
``{ "newsapi": {"last_at": "...", "ok": true, "rows": N} , ... }``.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from storage.models import ControlState, MacroObservation

TELEMETRY_KEY = "data.ingest.telemetry"

# Canonical provider ids, env var names, and display labels (order = dashboard order).
# News headline sources + FRED (macro). ``POLYGON_API_KEY`` is for market data (SIP) — not wired
# into ``ingest_news``; omit from this list to avoid a perpetually "never" row.
NEWS_DATA_PROVIDERS: tuple[tuple[str, str, str], ...] = (
    ("newsapi", "NEWS_API_KEY", "NewsAPI"),
    ("alphavantage", "ALPHAVANTAGE_API_KEY", "Alpha Vantage"),
    ("finnhub", "FINNHUB_API_KEY", "Finnhub"),
    ("marketaux", "MARKETAUX_API_TOKEN", "Marketaux"),
    ("fred", "FRED_API_KEY", "FRED"),
)


def _clean_api_key(raw: str | None) -> str:
    v = (raw or "").strip()
    if not v or v.startswith("#"):
        return ""
    return v


def _age_label_from_iso(iso: str | None) -> str:
    if not iso or not str(iso).strip():
        return "—"
    try:
        t = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        secs = max(0.0, (now - t).total_seconds())
        if secs < 60:
            return f"{int(secs)}s ago"
        if secs < 3600:
            return f"{int(secs // 60)}m ago"
        if secs < 86400:
            return f"{secs / 3600.0:.1f}h ago"
        return f"{int(secs // 86400)}d ago"
    except Exception:  # noqa: BLE001
        return "—"


def _state_from_last_at(
    last_iso: str | None,
    *,
    configured: bool,
    has_rows: bool,
) -> str:
    if not configured:
        return "off"
    if not has_rows or not last_iso:
        return "never"
    try:
        t = datetime.fromisoformat(str(last_iso).replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        hours = (now - t).total_seconds() / 3600.0
    except Exception:  # noqa: BLE001
        return "never"
    if hours <= 36.0:
        return "live"
    if hours <= 168.0:  # 7d
        return "stale"
    return "stale"


async def record_provider_ingest(
    session_factory: async_sessionmaker[AsyncSession] | None,
    provider: str,
    *,
    ok: bool = True,
    rows: int = 0,
    error: str | None = None,
) -> None:
    if session_factory is None:
        return
    now = datetime.now(timezone.utc).isoformat()
    pid = str(provider or "").strip().lower()
    if not pid:
        return
    async with session_factory() as session:
        q = await session.execute(select(ControlState).where(ControlState.key == TELEMETRY_KEY).limit(1))
        row = q.scalars().first()
        blob: dict[str, Any] = dict(row.value) if row and isinstance(row.value, dict) else {}
        entry: dict[str, Any] = {
            "last_at": now,
            "ok": bool(ok),
            "rows": int(max(0, rows)),
        }
        if error:
            entry["error"] = str(error)[:2000]
        else:
            entry["error"] = None
        blob[pid] = entry
        ts = datetime.now(timezone.utc)
        if row is None:
            session.add(ControlState(key=TELEMETRY_KEY, value=blob, updated_at=ts))
        else:
            row.value = blob
            row.updated_at = ts
        await session.commit()


async def _fred_latest_fetched_at(session: AsyncSession) -> datetime | None:
    r = await session.execute(select(func.max(MacroObservation.fetched_at)))
    v = r.scalar()
    if v is None:
        return None
    if hasattr(v, "replace"):
        dt = v
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt
    return None


def _best_iso(
    a: str | None,
    b: datetime | None,
) -> str | None:
    """Return the more recent of ISO string ``a`` or ``datetime`` ``b``."""
    dta: datetime | None = None
    if a and str(a).strip():
        try:
            dta = datetime.fromisoformat(str(a).replace("Z", "+00:00"))
            if dta.tzinfo is None:
                dta = dta.replace(tzinfo=timezone.utc)
        except Exception:  # noqa: BLE001
            dta = None
    dtb: datetime | None = None
    if b is not None:
        dtb = b if b.tzinfo else b.replace(tzinfo=timezone.utc)
    if dta and dtb:
        return dta if dta >= dtb else dtb.isoformat()
    if dta:
        return dta.isoformat()
    if dtb:
        return dtb.isoformat()
    return None


def build_news_data_provider_status_env_only() -> list[dict[str, Any]]:
    """
    No database (or as a last-resort fallback on DB errors). Still emit one
    row per :data:`NEWS_DATA_PROVIDERS` with ``configured``/``off``/``never`` from env only — so
    ``/system/status`` always lists the same set as
    :func:`build_news_data_provider_status` without reading ``ControlState``.
    """
    out: list[dict[str, Any]] = []
    for pid, env_name, label in NEWS_DATA_PROVIDERS:
        configured = bool(_clean_api_key(os.getenv(env_name)))
        state = "off" if not configured else "never"
        out.append(
            {
                "id": pid,
                "label": label,
                "configured": configured,
                "state": state,
                "last_ingest_at": None,
                "age_label": "—",
                "ok": True,
                "error": None,
            }
        )
    return out


async def build_news_data_provider_status(
    session_factory: async_sessionmaker[AsyncSession],
) -> list[dict[str, Any]]:
    """
    One row per configured provider in :data:`NEWS_DATA_PROVIDERS` for ``/system/status``.

    * ``off`` — API key not set
    * ``never`` — key set but no successful ingest recorded
    * ``live`` / ``stale`` — from last ingest age (FRED also uses DB ``macro_observations``).
    """
    blob: dict[str, Any] = {}
    async with session_factory() as session:
        q = await session.execute(select(ControlState).where(ControlState.key == TELEMETRY_KEY).limit(1))
        r = q.scalars().first()
        if r and isinstance(r.value, dict):
            blob = dict(r.value)
        fred_max = await _fred_latest_fetched_at(session)

    out: list[dict[str, Any]] = []
    for pid, env_name, label in NEWS_DATA_PROVIDERS:
        configured = bool(_clean_api_key(os.getenv(env_name)))
        pr = blob.get(pid)
        raw: dict[str, Any] = pr if isinstance(pr, dict) else {}
        last_iso: str | None = raw.get("last_at") if isinstance(raw.get("last_at"), str) else None
        if last_iso and not str(last_iso).strip():
            last_iso = None
        err: str | None = (str(raw.get("error"))[:2000] if raw.get("error") else None) or None
        if pid == "fred" and fred_max is not None:
            last_iso = _best_iso(last_iso, fred_max)
        has_rows = last_iso is not None
        state = _state_from_last_at(last_iso, configured=configured, has_rows=has_rows)
        if configured and has_rows and raw and raw.get("ok") is False and err:
            state = "error"
        ok_flag = bool(raw.get("ok", True)) if raw else True
        out.append(
            {
                "id": pid,
                "label": label,
                "configured": configured,
                "state": state,
                "last_ingest_at": last_iso,
                "age_label": _age_label_from_iso(last_iso) if has_rows else "—",
                "ok": ok_flag,
                "error": err,
            }
        )
    return out
