from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from loguru import logger

from control.command_bus import CommandBus, RISK_OVERRIDES_STATE_KEY
from storage.models import ControlCommand


async def hydrate_risk_parameters_from_bus(bus: CommandBus, risk_engine) -> None:
    """Restore dashboard/API regime overrides from control_state after ParameterManager + YAML load."""
    raw = await bus.get_state(RISK_OVERRIDES_STATE_KEY, None)
    if not isinstance(raw, dict):
        return
    pm = risk_engine._parameters
    for name, entry in raw.items():
        if not isinstance(entry, dict):
            continue
        val = entry.get("value")
        if val is None:
            continue
        reason = str(entry.get("reason", "control_state restore"))
        try:
            pm.apply_regime_override(str(name), float(val), reason, "db_restore")
        except Exception as exc:  # noqa: BLE001
            logger.warning("runner_control | skip restore risk param | {} | {}", name, exc)


async def apply_control_commands(
    bus: CommandBus,
    *,
    risk_engine,
    execution_engine,
    strategies: dict[str, Any],
) -> None:
    rows = await bus.claim_pending(limit=50)
    for row in rows:
        try:
            payload = row.payload or {}
            if row.command_type == "kill":
                raw_brokers = (payload or {}).get("brokers")
                if isinstance(raw_brokers, list) and raw_brokers:
                    if risk_engine is not None:
                        for b in raw_brokers:
                            risk_engine.disable_broker(str(b))
                else:
                    if risk_engine is not None:
                        risk_engine.kill()
                    if execution_engine is not None:
                        await execution_engine.cancel_all()
            elif row.command_type == "reset_kill":
                raw_brokers = (payload or {}).get("brokers")
                if isinstance(raw_brokers, list) and raw_brokers:
                    if risk_engine is not None:
                        for b in raw_brokers:
                            risk_engine.enable_broker(str(b))
                else:
                    if risk_engine is not None:
                        risk_engine.reset_kill()
            elif row.command_type == "toggle_strategy":
                name = str(payload.get("name", "")).strip()
                enabled = bool(payload.get("enabled", True))
                strat = strategies.get(name)
                if strat is not None:
                    strat.enabled = enabled
            elif row.command_type == "set_parameter":
                name = str(payload.get("name", "")).strip()
                value = Decimal(str(payload.get("value")))
                reason = str(payload.get("reason", "api override"))
                if not name:
                    raise ValueError("missing parameter name")
                if risk_engine is None or not hasattr(risk_engine, "_parameters"):
                    raise RuntimeError("risk engine parameter manager unavailable")
                risk_engine._parameters.apply_regime_override(name, float(value), reason=reason, source="api")  # noqa: SLF001
                await bus.merge_risk_override_state(name, str(value), reason)
                risk_engine._parameters.persist_regime_overrides_to_yaml()  # noqa: SLF001
            else:
                logger.warning("runner_control | unknown command type={}", row.command_type)
            await bus.mark_done(int(row.id))
            await _emit_command_events(bus, row, risk_engine)
        except Exception as exc:  # noqa: BLE001
            await bus.mark_failed(int(row.id), str(exc))
            await bus.append_dashboard_event(
                "command_failed",
                {"command_id": int(row.id), "type": row.command_type, "error": str(exc)[:500]},
            )


async def _emit_command_events(bus: CommandBus, row: ControlCommand, risk_engine) -> None:
    cid = int(row.id)
    ctype = row.command_type
    if ctype == "kill":
        await bus.append_dashboard_event(
            "kill_activated",
            {"command_id": cid, "killed": bool(risk_engine and getattr(risk_engine, "is_killed", False))},
        )
    elif ctype == "reset_kill":
        await bus.append_dashboard_event(
            "kill_reset",
            {"command_id": cid, "killed": bool(risk_engine and getattr(risk_engine, "is_killed", False))},
        )
    elif ctype == "set_parameter":
        p = row.payload or {}
        await bus.append_dashboard_event(
            "command_completed",
            {
                "command_id": cid,
                "type": ctype,
                "parameter": p.get("name"),
                "value": p.get("value"),
            },
        )
    else:
        await bus.append_dashboard_event(
            "command_completed",
            {"command_id": cid, "type": ctype, "payload": row.payload or {}},
        )


async def publish_runner_heartbeat(
    bus: CommandBus,
    *,
    runner_name: str,
    symbols: list[str],
    generated: int,
    executed: int | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    payload = {
        "runner": runner_name,
        "symbols": symbols,
        "generated": generated,
        "executed": executed,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if extra:
        payload.update(extra)
    await bus.set_state("runtime.heartbeat", payload)
