from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from storage.models import ControlCommand, ControlState


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

    async def get_recent_commands(self, *, limit: int = 50) -> list[ControlCommand]:
        async with self._session_factory() as session:
            q = await session.execute(select(ControlCommand).order_by(ControlCommand.id.desc()).limit(limit))
            return list(q.scalars().all())

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
