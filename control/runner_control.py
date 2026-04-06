from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from loguru import logger

from control.command_bus import CommandBus


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
                if risk_engine is not None:
                    risk_engine.kill()
                if execution_engine is not None:
                    await execution_engine.cancel_all()
            elif row.command_type == "reset_kill":
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
            else:
                logger.warning("runner_control | unknown command type={}", row.command_type)
            await bus.mark_done(int(row.id))
        except Exception as exc:  # noqa: BLE001
            await bus.mark_failed(int(row.id), str(exc))


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
