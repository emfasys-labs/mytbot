"""Dashboard read-auth, WS event polling, and startup checks (kept out of server.py)."""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from control.command_bus import DASHBOARD_EVENTS_KEY, RISK_OVERRIDES_STATE_KEY
from storage.models import OrderLog, SignalLog

_log = logging.getLogger("uvicorn.error")

APP_ENV = os.getenv("APP_ENV", "paper")
ALLOWED_ORIGINS_RAW = os.getenv("API_ALLOWED_ORIGINS", "*").strip()

_ws_poll_state: dict[str, Any] = {"signal_ts": None, "order_ts": None}


def log_cors_live_warning() -> None:
    if APP_ENV != "live":
        return
    if ALLOWED_ORIGINS_RAW == "*" or not ALLOWED_ORIGINS_RAW:
        _log.warning(
            "api | APP_ENV=live but API_ALLOWED_ORIGINS is not a strict allow-list — "
            "set API_ALLOWED_ORIGINS to your dashboard origin(s) before production."
        )


def verify_dashboard_token(header_token: str | None, auth_bearer: str | None) -> bool:
    expected = os.getenv("DASHBOARD_READ_TOKEN", "").strip()
    if not expected:
        return True
    t = (header_token or "").strip()
    if not t and auth_bearer:
        t = auth_bearer.replace("Bearer ", "", 1).strip()
    return t == expected


async def merge_risk_parameters_for_api(pm, bus) -> dict[str, str]:
    """Merge YAML defaults with control_state overrides (runner may differ from API process)."""
    raw = await bus.get_state(RISK_OVERRIDES_STATE_KEY, None) if bus is not None else None
    out: dict[str, str] = {}
    for name in pm._cfg.get("risk_parameters", {}).keys():  # noqa: SLF001
        if isinstance(raw, dict) and name in raw and isinstance(raw[name], dict) and raw[name].get("value") is not None:
            out[name] = str(raw[name]["value"])
        else:
            out[name] = str(pm.get_value(name))
    return out


async def gather_ws_events(bus, session_factory) -> list[dict[str, Any]]:
    """Events for WebSocket: rolling bus log + new signals/orders from DB."""
    events: list[dict[str, Any]] = []
    if bus is not None:
        raw = await bus.get_state(DASHBOARD_EVENTS_KEY, [])
        if isinstance(raw, list) and raw:
            events.extend(raw[-15:])

    if session_factory is None:
        return events[-40:]

    now = datetime.now(timezone.utc)
    async with session_factory() as session:
        if _ws_poll_state["signal_ts"] is None:
            _ws_poll_state["signal_ts"] = now
        else:
            q = await session.execute(
                select(SignalLog)
                .where(SignalLog.timestamp > _ws_poll_state["signal_ts"])
                .order_by(SignalLog.timestamp.asc())
                .limit(15)
            )
            sig_rows = list(q.scalars().all())
            for r in sig_rows:
                events.append(
                    {
                        "type": "signal_generated",
                        "payload": {
                            "id": r.id,
                            "symbol": r.symbol,
                            "side": r.side,
                            "strategy": r.strategy,
                            "timestamp": r.timestamp.isoformat() if r.timestamp else None,
                        },
                        "ts": r.timestamp.isoformat() if r.timestamp else now.isoformat(),
                    }
                )
            if sig_rows:
                _ws_poll_state["signal_ts"] = sig_rows[-1].timestamp

        if _ws_poll_state["order_ts"] is None:
            _ws_poll_state["order_ts"] = now
        else:
            qo = await session.execute(
                select(OrderLog)
                .where(OrderLog.timestamp > _ws_poll_state["order_ts"])
                .order_by(OrderLog.timestamp.asc())
                .limit(25)
            )
            ord_rows = list(qo.scalars().all())
            for o in ord_rows:
                st = (o.status or "").lower()
                if st in ("filled", "partially_filled"):
                    events.append(
                        {
                            "type": "order_filled",
                            "payload": {
                                "id": o.id,
                                "symbol": o.symbol,
                                "status": o.status,
                                "filled_quantity": str(o.filled_quantity) if o.filled_quantity is not None else None,
                                "timestamp": o.timestamp.isoformat() if o.timestamp else None,
                            },
                            "ts": o.timestamp.isoformat() if o.timestamp else now.isoformat(),
                        }
                    )
            if ord_rows:
                _ws_poll_state["order_ts"] = ord_rows[-1].timestamp

    return events[-40:]
