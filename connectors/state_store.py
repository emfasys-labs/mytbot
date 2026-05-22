"""
connectors/state_store.py
==========================
D127 Connect Hub v2 — read/write layer for per-install connector state.

`config/connectors.yaml` is the static catalogue; this module is the
per-install state recorded in the `connector_state` table — lifecycle
status, last-test result, detected capabilities, certification tier, and
AI-stage version/install metadata.

All writes are upserts keyed on `(category, connector_id)`. Secrets are
never stored here.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import select

from storage.models import ConnectorState

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def state_to_dict(row: ConnectorState) -> dict[str, Any]:
    """Serialise a ConnectorState row to a JSON-friendly dict."""
    return {
        "category": row.category,
        "connector_id": row.connector_id,
        "status": row.status,
        "enabled": bool(row.enabled),
        "certification_tier": row.certification_tier,
        "last_test_at": row.last_test_at.isoformat() if row.last_test_at else None,
        "last_test_result": row.last_test_result,
        "detected_capabilities": row.detected_capabilities,
        "ai_model_version": row.ai_model_version,
        "local_model_install_state": row.local_model_install_state,
        "machine_probe": row.machine_probe,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


async def load_all_states(session_factory) -> dict[tuple[str, str], dict[str, Any]]:
    """Return every connector state keyed by `(category, connector_id)`."""
    if session_factory is None:
        return {}
    out: dict[tuple[str, str], dict[str, Any]] = {}
    async with session_factory() as session:
        rows = (await session.execute(select(ConnectorState))).scalars().all()
        for row in rows:
            out[(row.category, row.connector_id)] = state_to_dict(row)
    return out


async def load_state(
    session_factory, category: str, connector_id: str
) -> Optional[dict[str, Any]]:
    """Return one connector's state, or None if it has never been persisted."""
    if session_factory is None:
        return None
    cat = (category or "").strip().lower()
    cid = (connector_id or "").strip().lower()
    async with session_factory() as session:
        row = (
            await session.execute(
                select(ConnectorState).where(
                    ConnectorState.category == cat,
                    ConnectorState.connector_id == cid,
                )
            )
        ).scalars().first()
        return state_to_dict(row) if row is not None else None


async def upsert_state(
    session_factory,
    *,
    category: str,
    connector_id: str,
    status: Optional[str] = None,
    enabled: Optional[bool] = None,
    certification_tier: Optional[str] = None,
    last_test_at: Optional[datetime] = None,
    last_test_result: Optional[dict] = None,
    detected_capabilities: Optional[dict] = None,
    ai_model_version: Optional[str] = None,
    local_model_install_state: Optional[str] = None,
    machine_probe: Optional[dict] = None,
) -> Optional[dict[str, Any]]:
    """Create or update a connector_state row. Only non-None fields change.

    Returns the resulting state dict, or None when the DB is unavailable.
    """
    if session_factory is None:
        return None
    cat = (category or "").strip().lower()
    cid = (connector_id or "").strip().lower()
    if not cat or not cid:
        return None
    try:
        async with session_factory() as session:
            row = (
                await session.execute(
                    select(ConnectorState).where(
                        ConnectorState.category == cat,
                        ConnectorState.connector_id == cid,
                    )
                )
            ).scalars().first()
            if row is None:
                row = ConnectorState(
                    category=cat,
                    connector_id=cid,
                    status=status or "not_configured",
                    enabled=bool(enabled) if enabled is not None else False,
                    updated_at=_now(),
                )
                session.add(row)
            if status is not None:
                row.status = status
            if enabled is not None:
                row.enabled = bool(enabled)
            if certification_tier is not None:
                row.certification_tier = certification_tier
            if last_test_at is not None:
                row.last_test_at = last_test_at
            if last_test_result is not None:
                row.last_test_result = last_test_result
            if detected_capabilities is not None:
                row.detected_capabilities = detected_capabilities
            if ai_model_version is not None:
                row.ai_model_version = ai_model_version
            if local_model_install_state is not None:
                row.local_model_install_state = local_model_install_state
            if machine_probe is not None:
                row.machine_probe = machine_probe
            row.updated_at = _now()
            await session.commit()
            await session.refresh(row)
            return state_to_dict(row)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "connector state upsert failed | %s/%s | %s", cat, cid, exc
        )
        return None
