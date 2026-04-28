from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete, select

from storage.models import ControlCommand, ControlState

DASHBOARD_EVENTS_KEY = "dashboard.events"
RISK_OVERRIDES_STATE_KEY = "risk.parameters.override"
CAPITAL_ALLOCATION_STATE_KEY = "system.capital_allocation"


class CommandBus:
    def __init__(self, session_factory):
        self._session_factory = session_factory

    async def enqueue(self, command_type: str, payload: dict[str, Any] | None = None, *, source: str = "api") -> int:
        async with self._session_factory() as session:
            row = ControlCommand(
                command_type=command_type,
                payload=payload or {},
                status="pending",
                source=source,
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return int(row.id)

    async def claim_pending_of_type(self, command_type: str, *, limit: int = 20) -> list[ControlCommand]:
        """Claim only rows matching command_type (leaves other pending commands untouched)."""
        out: list[ControlCommand] = []
        async with self._session_factory() as session:
            q = await session.execute(
                select(ControlCommand)
                .where(ControlCommand.status == "pending", ControlCommand.command_type == command_type)
                .order_by(ControlCommand.id.asc())
                .limit(limit)
            )
            rows = list(q.scalars().all())
            now = datetime.now(timezone.utc)
            for row in rows:
                row.status = "processing"
                row.claimed_at = now
                out.append(row)
            await session.commit()
            for row in out:
                await session.refresh(row)
        return out

    async def claim_pending(self, *, limit: int = 20) -> list[ControlCommand]:
        out: list[ControlCommand] = []
        async with self._session_factory() as session:
            q = await session.execute(
                select(ControlCommand)
                .where(ControlCommand.status == "pending")
                .order_by(ControlCommand.id.asc())
                .limit(limit)
            )
            rows = list(q.scalars().all())
            now = datetime.now(timezone.utc)
            for row in rows:
                row.status = "processing"
                row.claimed_at = now
                out.append(row)
            await session.commit()
            for row in out:
                await session.refresh(row)
        return out

    async def mark_done(self, command_id: int) -> None:
        async with self._session_factory() as session:
            q = await session.execute(select(ControlCommand).where(ControlCommand.id == command_id).limit(1))
            row = q.scalars().first()
            if row is None:
                return
            row.status = "done"
            row.processed_at = datetime.now(timezone.utc)
            row.error = None
            await session.commit()

    async def mark_failed(self, command_id: int, error: str) -> None:
        async with self._session_factory() as session:
            q = await session.execute(select(ControlCommand).where(ControlCommand.id == command_id).limit(1))
            row = q.scalars().first()
            if row is None:
                return
            row.status = "failed"
            row.processed_at = datetime.now(timezone.utc)
            row.error = error[:4000]
            await session.commit()

    async def delete_pending_commands_of_type(self, command_type: str) -> int:
        """Remove queued control rows so runners do not execute stale kills (local recovery)."""
        async with self._session_factory() as session:
            r = await session.execute(
                delete(ControlCommand).where(
                    ControlCommand.command_type == command_type,
                    ControlCommand.status.in_(["pending", "processing"]),
                )
            )
            await session.commit()
            return int(r.rowcount or 0)

    async def get_recent_commands(self, *, limit: int = 50) -> list[ControlCommand]:
        async with self._session_factory() as session:
            q = await session.execute(select(ControlCommand).order_by(ControlCommand.id.desc()).limit(limit))
            return list(q.scalars().all())

    async def get_command(self, command_id: int) -> ControlCommand | None:
        async with self._session_factory() as session:
            q = await session.execute(select(ControlCommand).where(ControlCommand.id == command_id).limit(1))
            return q.scalars().first()

    async def append_dashboard_event(self, event_type: str, payload: dict[str, Any] | None = None) -> None:
        """Rolling event log for WebSocket push (max 100 entries)."""
        async with self._session_factory() as session:
            q = await session.execute(select(ControlState).where(ControlState.key == DASHBOARD_EVENTS_KEY).limit(1))
            row = q.scalars().first()
            lst: list[dict[str, Any]] = []
            if row is not None and isinstance(row.value, list):
                lst = list(row.value)
            entry = {
                "type": event_type,
                "payload": payload or {},
                "ts": datetime.now(timezone.utc).isoformat(),
            }
            lst.append(entry)
            lst = lst[-100:]
            if row is None:
                session.add(ControlState(key=DASHBOARD_EVENTS_KEY, value=lst, updated_at=datetime.now(timezone.utc)))
            else:
                row.value = lst
                row.updated_at = datetime.now(timezone.utc)
            await session.commit()

    async def merge_risk_override_state(self, name: str, value: str, reason: str) -> None:
        """Persist effective risk overrides for API display and runner restarts."""
        async with self._session_factory() as session:
            q = await session.execute(select(ControlState).where(ControlState.key == RISK_OVERRIDES_STATE_KEY).limit(1))
            row = q.scalars().first()
            data: dict[str, Any] = {}
            if row is not None and isinstance(row.value, dict):
                data = dict(row.value)
            data[name] = {"value": value, "reason": reason, "updated_at": datetime.now(timezone.utc).isoformat()}
            if row is None:
                session.add(
                    ControlState(key=RISK_OVERRIDES_STATE_KEY, value=data, updated_at=datetime.now(timezone.utc))
                )
            else:
                row.value = data
                row.updated_at = datetime.now(timezone.utc)
            await session.commit()

    async def set_state(self, key: str, value: Any) -> None:
        async with self._session_factory() as session:
            q = await session.execute(select(ControlState).where(ControlState.key == key).limit(1))
            row = q.scalars().first()
            if row is None:
                row = ControlState(key=key, value=value, updated_at=datetime.now(timezone.utc))
                session.add(row)
            else:
                row.value = value
                row.updated_at = datetime.now(timezone.utc)
            await session.commit()

    async def get_state(self, key: str, default: Any = None) -> Any:
        async with self._session_factory() as session:
            q = await session.execute(select(ControlState).where(ControlState.key == key).limit(1))
            row = q.scalars().first()
            if row is None:
                return default
            return row.value

    async def get_state_prefix(self, prefix: str) -> dict[str, Any]:
        async with self._session_factory() as session:
            q = await session.execute(select(ControlState).where(ControlState.key.like(f"{prefix}%")))
            rows = list(q.scalars().all())
            return {str(r.key): r.value for r in rows}
