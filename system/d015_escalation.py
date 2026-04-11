"""D015 volume escalation enqueue + feature refresh via CommandBus."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from config.models import AllocationConfig
from control.command_bus import CommandBus
from data.feature_lookup import load_latest_features_for_symbols
from portfolio.d015_replacement_context import ReplacementContext
from sqlalchemy.ext.asyncio import AsyncSession

D015_REPLACEMENT_STATE_KEY = "d015.replacement_history"
D015_SMOOTHING_PREV_KEY = "d015.allocation_smoothing_prev"


async def enqueue_volume_escalation_symbols(
    bus: CommandBus,
    symbols: list[str],
    allocation_cfg: AllocationConfig,
) -> None:
    ve = allocation_cfg.volume_escalation
    if not ve.enabled or not symbols:
        return
    syms = [str(s).strip()[:32] for s in symbols if s and str(s).strip()]
    if not syms:
        return
    await bus.enqueue(
        ve.command_type,
        {"symbols": list(dict.fromkeys(syms)), "ts": datetime.now(timezone.utc).isoformat()},
        source="d015",
    )


async def drain_volume_refresh_features(
    session: AsyncSession,
    bus: CommandBus,
    *,
    universe_symbols: list[str],
    timeframe: str,
    allocation_cfg: AllocationConfig,
) -> dict[str, dict]:
    """Claim pending volume-refresh commands and merge latest DB features for those symbols."""
    ve = allocation_cfg.volume_escalation
    rows = await bus.claim_pending_of_type(ve.command_type, limit=40)
    extra_syms: set[str] = set()
    for row in rows:
        pl = row.payload or {}
        for s in pl.get("symbols") or []:
            if s:
                extra_syms.add(str(s).strip()[:32])
        try:
            await bus.mark_done(int(row.id))
        except Exception:  # noqa: BLE001
            try:
                await bus.mark_failed(int(row.id), "d015_volume_refresh_done")
            except Exception:  # noqa: BLE001
                pass
    merged = list(dict.fromkeys(list(universe_symbols) + list(extra_syms)))
    if not merged:
        return {}
    return await load_latest_features_for_symbols(session, merged, timeframe)


async def load_replacement_context_from_bus(bus: CommandBus) -> ReplacementContext:
    raw = await bus.get_state(D015_REPLACEMENT_STATE_KEY, None)
    return ReplacementContext.from_control_value(raw)


async def save_replacement_context_to_bus(bus: CommandBus, ctx: ReplacementContext) -> None:
    await bus.set_state(D015_REPLACEMENT_STATE_KEY, ctx.to_control_value())


async def load_smoothing_prev_from_bus(bus: CommandBus) -> dict[str, Any] | None:
    raw = await bus.get_state(D015_SMOOTHING_PREV_KEY, None)
    return raw if isinstance(raw, dict) else None


async def save_smoothing_prev_to_bus(bus: CommandBus, snapshot: dict[str, Any]) -> None:
    await bus.set_state(D015_SMOOTHING_PREV_KEY, snapshot)


def merge_replacement_events_from_decision(
    ctx: ReplacementContext,
    *,
    decision: Any,
    now: datetime,
) -> None:
    from core.models_runtime import AllocationDecision

    if not isinstance(decision, AllocationDecision):
        return
    ts = now.astimezone(timezone.utc)
    for r in decision.replacement_candidates:
        ctx.last_event_at_by_symbol[str(r.old_symbol).strip()[:32]] = ts
        ctx.recent_events.append(
            {
                "old": str(r.old_symbol).strip()[:32],
                "new": str(r.new_symbol).strip()[:32],
                "ts": ts.isoformat(),
            }
        )
    cap = 50
    if len(ctx.recent_events) > cap:
        ctx.recent_events = ctx.recent_events[-cap:]
