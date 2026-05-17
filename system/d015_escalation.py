"""D015 volume escalation enqueue + feature refresh via CommandBus."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from loguru import logger

from config.models import AllocationConfig
from control.command_bus import CommandBus
from data.feature_lookup import load_latest_features_for_symbols
from portfolio.d015_replacement_context import ReplacementContext
from sqlalchemy.ext.asyncio import AsyncSession

D015_REPLACEMENT_STATE_KEY = "d015.replacement_history"
D015_SMOOTHING_PREV_KEY = "d015.allocation_smoothing_prev"

# Durable local mirror of the anti-churn replacement context. The primary
# store is the DB-backed CommandBus (ControlState). But on a process restart
# while Postgres is briefly unavailable/empty (e.g. the post machine-wake or
# Docker-restart window), ``bus.get_state`` can yield ``None`` — which would
# silently reset every cull/re-entry cooldown and let the allocator
# immediately re-open names it just culled (the close→reopen bleed). This
# file mirror lets those cooldowns survive even a total DB outage at boot.
_REPL_CTX_MIRROR = Path(
    os.getenv(
        "MYTBOT_REPLACEMENT_CONTEXT_FILE",
        "data/runtime/replacement_context.json",
    )
)


def _ctx_value_is_empty(raw: object | None) -> bool:
    """True when a control-value carries no usable churn/cooldown history."""
    if not isinstance(raw, dict):
        return True
    return not (
        raw.get("last_event_at_by_symbol")
        or raw.get("last_cull_at_by_symbol")
        or raw.get("recent_events")
    )


def _read_repl_ctx_mirror() -> dict[str, Any] | None:
    try:
        if not _REPL_CTX_MIRROR.exists():
            return None
        data = json.loads(_REPL_CTX_MIRROR.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception as exc:  # noqa: BLE001 — mirror is best-effort
        logger.debug("replacement_context | mirror read failed | {}", exc)
        return None


def _write_repl_ctx_mirror(value: dict[str, Any]) -> None:
    try:
        _REPL_CTX_MIRROR.parent.mkdir(parents=True, exist_ok=True)
        # Atomic write so a crash mid-write never leaves a corrupt mirror.
        fd, tmp = tempfile.mkstemp(
            prefix=".repl_ctx_", suffix=".json", dir=str(_REPL_CTX_MIRROR.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(value, fh)
            os.replace(tmp, _REPL_CTX_MIRROR)
        finally:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass
    except Exception as exc:  # noqa: BLE001 — never break trading on mirror IO
        logger.debug("replacement_context | mirror write failed | {}", exc)


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
    """Load the anti-churn context, preferring the DB bus but falling back to
    the durable local mirror when the bus is empty/unavailable.

    A restart during a DB-outage window must NOT silently drop cull /
    re-entry cooldowns — that is exactly what lets the allocator re-open a
    just-culled symbol and bleed spread+fees. If the bus yields nothing
    usable, restore from the on-disk mirror so the brakes stay on.
    """
    raw: object | None
    try:
        raw = await bus.get_state(D015_REPLACEMENT_STATE_KEY, None)
    except Exception as exc:  # noqa: BLE001 — DB hiccup must not wipe cooldowns
        logger.warning(
            "replacement_context | bus read failed ({}) — restoring from mirror",
            exc,
        )
        raw = None
    if _ctx_value_is_empty(raw):
        mirror = _read_repl_ctx_mirror()
        if not _ctx_value_is_empty(mirror):
            logger.info(
                "replacement_context | bus empty/unavailable — restored "
                "cull/re-entry cooldowns from durable mirror"
            )
            raw = mirror
    return ReplacementContext.from_control_value(raw)


async def save_replacement_context_to_bus(bus: CommandBus, ctx: ReplacementContext) -> None:
    value = ctx.to_control_value()
    # Mirror first: it must persist even if the DB write below raises, so a
    # crash right after this point still preserves the cooldowns.
    _write_repl_ctx_mirror(value)
    await bus.set_state(D015_REPLACEMENT_STATE_KEY, value)


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
