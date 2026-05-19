"""Persistence layer for the instrument registry (D116).

Pure async upsert/query helpers over the four D116 tables. Designed to be
called from source adapters, the builder, the per-broker availability
resolver, the API, and the CLI scripts.

Never deletes rows. Retirement is signalled by stamping
``InstrumentRegistry.retired_at`` only after consensus across multiple
independent sources.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Optional, Sequence

from loguru import logger
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from instruments.canonical import AssetClassHint, CanonicalSymbol, to_canonical
from storage.models import (
    InstrumentBrokerAvailability,
    InstrumentRegistry,
    InstrumentSourceMembership,
    InstrumentSourceRun,
)


AVAILABILITY_STATES = frozenset(
    ("unknown", "available", "unavailable", "requires_qualification", "blocked")
)


@dataclass(frozen=True)
class RegistryRow:
    """In-memory view of an ``InstrumentRegistry`` row."""

    canonical_symbol: str
    display_name: Optional[str]
    asset_class: AssetClassHint
    region: Optional[str]
    exchange: Optional[str]
    currency: Optional[str]
    sector: Optional[str]
    industry: Optional[str]
    isin: Optional[str]
    figi: Optional[str]
    first_seen_at: datetime
    last_seen_at: datetime
    last_refreshed_at: Optional[datetime]
    retired_at: Optional[datetime]
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AvailabilityRow:
    canonical_symbol: str
    broker: str
    broker_symbol: Optional[str]
    status: str
    last_checked_at: datetime
    last_available_at: Optional[datetime]
    last_error: Optional[str] = None
    qualification_payload: Optional[Mapping[str, Any]] = None


@dataclass(frozen=True)
class SourceContribution:
    """One symbol contributed by a source during a refresh."""

    canonical_symbol: str
    display_name: Optional[str] = None
    asset_class: AssetClassHint = "equity"
    region: Optional[str] = None
    exchange: Optional[str] = None
    currency: Optional[str] = None
    sector: Optional[str] = None
    industry: Optional[str] = None
    isin: Optional[str] = None
    figi: Optional[str] = None
    external_id: Optional[str] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class UpsertSummary:
    rows_added: int
    rows_updated: int
    rows_missing: int
    contributions: int
    skipped: int


def _now() -> datetime:
    return datetime.now(timezone.utc)


def coerce_contribution(
    raw_symbol: str,
    *,
    asset_class_hint: Optional[AssetClassHint] = None,
    region_hint: Optional[str] = None,
    display_name: Optional[str] = None,
    exchange: Optional[str] = None,
    currency: Optional[str] = None,
    sector: Optional[str] = None,
    industry: Optional[str] = None,
    isin: Optional[str] = None,
    figi: Optional[str] = None,
    external_id: Optional[str] = None,
    metadata: Optional[Mapping[str, Any]] = None,
    broker: Optional[str] = None,
) -> Optional[SourceContribution]:
    """Normalise a raw source row into a SourceContribution, or ``None`` if invalid."""
    parsed: Optional[CanonicalSymbol] = to_canonical(
        raw_symbol,
        broker=broker,
        asset_class_hint=asset_class_hint,
        region_hint=region_hint,
    )
    if parsed is None:
        return None
    return SourceContribution(
        canonical_symbol=parsed.symbol,
        display_name=(display_name or "").strip() or None,
        asset_class=asset_class_hint or parsed.asset_class,
        region=region_hint or parsed.region,
        exchange=exchange or parsed.exchange,
        currency=(currency or "").strip().upper() or None,
        sector=(sector or "").strip() or None,
        industry=(industry or "").strip() or None,
        isin=(isin or "").strip().upper() or None,
        figi=(figi or "").strip().upper() or None,
        external_id=(external_id or "").strip() or None,
        metadata=dict(metadata or {}),
    )


async def upsert_contributions(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    source_id: str,
    source_version: str,
    contributions: Sequence[SourceContribution],
    retire_policy: Optional[Mapping[str, int]] = None,
) -> UpsertSummary:
    """Upsert source contributions into the registry and membership tables.

    - Adds or updates ``InstrumentRegistry`` per canonical symbol.
    - Resets ``consecutive_miss_count`` to 0 for contributed symbols and
      increments it for previously-known-but-now-missing symbols of the same
      source.
    - Never deletes; retirement only stamps ``retired_at`` once N misses are
      seen across at least M independent sources.
    """
    rows_added = 0
    rows_updated = 0
    rows_missing = 0
    contributed: set[str] = set()
    skipped = 0
    seen_contributions: list[SourceContribution] = []
    for c in contributions:
        sym = (c.canonical_symbol or "").strip().upper()
        if not sym:
            skipped += 1
            continue
        if sym in contributed:
            continue  # de-dupe within the same batch
        contributed.add(sym)
        seen_contributions.append(c)

    now = _now()
    async with session_factory() as session:
        async with session.begin():
            # Registry upsert (per symbol)
            existing_rows = await _load_registry_subset(session, contributed)
            for c in seen_contributions:
                existed = c.canonical_symbol in existing_rows
                values: dict[str, Any] = {
                    "canonical_symbol": c.canonical_symbol,
                    "display_name": c.display_name,
                    "asset_class": c.asset_class,
                    "region": c.region,
                    "exchange": c.exchange,
                    "currency": c.currency,
                    "sector": c.sector,
                    "industry": c.industry,
                    "isin": c.isin,
                    "figi": c.figi,
                    "first_seen_at": now,
                    "last_seen_at": now,
                    "last_refreshed_at": now,
                    "retired_at": None,
                    "metadata": dict(c.metadata or {}),
                }
                # NOTE: pg_insert is called against the underlying ``__table__``
                # rather than the ORM class so column names use the database
                # column ``metadata`` instead of the mapped attribute
                # ``metadata_``. Passing the ORM class would otherwise trigger
                # ``'MetaData' object has no attribute '_bulk_update_tuples'``
                # because SQLAlchemy resolves ``Model.metadata`` to the global
                # MetaData object first.
                stmt = pg_insert(InstrumentRegistry.__table__).values(**values)
                update_set: dict[str, Any] = {
                    "last_seen_at": now,
                    "last_refreshed_at": now,
                    "retired_at": None,
                }
                for col in (
                    "display_name",
                    "asset_class",
                    "region",
                    "exchange",
                    "currency",
                    "sector",
                    "industry",
                    "isin",
                    "figi",
                ):
                    if values[col] is not None:
                        update_set[col] = values[col]
                stmt = stmt.on_conflict_do_update(
                    index_elements=["canonical_symbol"],
                    set_=update_set,
                )
                await session.execute(stmt)
                if existed:
                    rows_updated += 1
                else:
                    rows_added += 1

            # Membership upsert (per (symbol, source)). Use ``__table__`` for
            # the same reason as the registry upsert above; the underlying
            # database column is named ``metadata`` even though the ORM mapped
            # attribute is ``metadata_``.
            for c in seen_contributions:
                stmt = pg_insert(InstrumentSourceMembership.__table__).values(
                    canonical_symbol=c.canonical_symbol,
                    source_id=source_id,
                    source_version=source_version,
                    external_id=c.external_id,
                    last_seen_at=now,
                    consecutive_miss_count=0,
                    metadata=dict(c.metadata or {}),
                )
                stmt = stmt.on_conflict_do_update(
                    index_elements=["canonical_symbol", "source_id"],
                    set_={
                        "source_version": source_version,
                        "external_id": c.external_id,
                        "last_seen_at": now,
                        "consecutive_miss_count": 0,
                        "metadata": dict(c.metadata or {}),
                    },
                )
                await session.execute(stmt)

            # Increment misses for previously-tracked symbols that did not appear
            missing_symbols = await _missing_for_source(session, source_id, contributed)
            rows_missing = len(missing_symbols)
            if missing_symbols:
                await session.execute(
                    update(InstrumentSourceMembership)
                    .where(InstrumentSourceMembership.source_id == source_id)
                    .where(InstrumentSourceMembership.canonical_symbol.in_(list(missing_symbols)))
                    .values(consecutive_miss_count=InstrumentSourceMembership.consecutive_miss_count + 1)
                )

    # Retire policy after every refresh; pure SQL pass so it stays cheap.
    if retire_policy:
        try:
            await apply_retire_policy(session_factory, retire_policy=retire_policy)
        except Exception as exc:  # noqa: BLE001
            logger.debug("instruments.registry | retire_policy non-fatal: {}", exc)

    return UpsertSummary(
        rows_added=rows_added,
        rows_updated=rows_updated,
        rows_missing=rows_missing,
        contributions=len(seen_contributions),
        skipped=skipped,
    )


async def _load_registry_subset(
    session: AsyncSession, symbols: Iterable[str]
) -> set[str]:
    syms = [s for s in symbols if s]
    if not syms:
        return set()
    rows = await session.execute(
        select(InstrumentRegistry.canonical_symbol).where(
            InstrumentRegistry.canonical_symbol.in_(syms)
        )
    )
    return {r[0] for r in rows.all()}


async def _missing_for_source(
    session: AsyncSession, source_id: str, contributed: set[str]
) -> list[str]:
    rows = await session.execute(
        select(InstrumentSourceMembership.canonical_symbol).where(
            InstrumentSourceMembership.source_id == source_id
        )
    )
    existing = {r[0] for r in rows.all()}
    return sorted(existing - contributed)


async def apply_retire_policy(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    retire_policy: Mapping[str, int],
) -> int:
    """Stamp ``InstrumentRegistry.retired_at`` for symbols missing across many sources.

    Returns the number of rows newly retired.
    """
    min_consecutive_misses = int(retire_policy.get("min_consecutive_misses", 5) or 5)
    min_sources_missing = int(retire_policy.get("min_sources_missing", 2) or 2)
    now = _now()
    async with session_factory() as session:
        async with session.begin():
            rows = await session.execute(
                select(
                    InstrumentSourceMembership.canonical_symbol,
                    InstrumentSourceMembership.source_id,
                    InstrumentSourceMembership.consecutive_miss_count,
                )
            )
            misses_by_symbol: dict[str, int] = {}
            for sym, _src, miss in rows.all():
                if miss is None or miss < min_consecutive_misses:
                    continue
                misses_by_symbol[sym] = misses_by_symbol.get(sym, 0) + 1
            to_retire = [
                sym for sym, count in misses_by_symbol.items() if count >= min_sources_missing
            ]
            if not to_retire:
                return 0
            await session.execute(
                update(InstrumentRegistry)
                .where(InstrumentRegistry.canonical_symbol.in_(to_retire))
                .where(InstrumentRegistry.retired_at.is_(None))
                .values(retired_at=now)
            )
            return len(to_retire)


async def record_source_run(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    source_id: str,
    started_at: datetime,
    finished_at: Optional[datetime],
    status: str,
    rows_added: Optional[int] = None,
    rows_updated: Optional[int] = None,
    rows_missing: Optional[int] = None,
    notes: Optional[str] = None,
    error: Optional[str] = None,
) -> None:
    async with session_factory() as session:
        async with session.begin():
            session.add(
                InstrumentSourceRun(
                    source_id=source_id,
                    started_at=started_at,
                    finished_at=finished_at,
                    status=status,
                    rows_added=rows_added,
                    rows_updated=rows_updated,
                    rows_missing=rows_missing,
                    notes=notes,
                    error=error,
                )
            )


async def upsert_broker_availability(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    broker: str,
    rows: Sequence[AvailabilityRow],
) -> int:
    if not rows:
        return 0
    now = _now()
    async with session_factory() as session:
        async with session.begin():
            for r in rows:
                if r.status not in AVAILABILITY_STATES:
                    logger.debug(
                        "instruments.registry | invalid status '{}' skipped for {}/{}",
                        r.status, broker, r.canonical_symbol,
                    )
                    continue
                values = {
                    "canonical_symbol": r.canonical_symbol,
                    "broker": broker,
                    "broker_symbol": r.broker_symbol,
                    "status": r.status,
                    "last_checked_at": now,
                    "last_available_at": (
                        now if r.status == "available" else r.last_available_at
                    ),
                    "qualification_payload": dict(r.qualification_payload)
                    if r.qualification_payload
                    else None,
                    "last_error": r.last_error,
                }
                # Use ``__table__`` for consistency with the other registry
                # upserts; tolerates ORM attribute-vs-column aliasing safely.
                stmt = pg_insert(InstrumentBrokerAvailability.__table__).values(**values)
                set_update = {
                    "broker_symbol": values["broker_symbol"],
                    "status": values["status"],
                    "last_checked_at": now,
                    "qualification_payload": values["qualification_payload"],
                    "last_error": values["last_error"],
                }
                if r.status == "available":
                    set_update["last_available_at"] = now
                stmt = stmt.on_conflict_do_update(
                    index_elements=["canonical_symbol", "broker"],
                    set_=set_update,
                )
                await session.execute(stmt)
    return len(rows)


async def list_active_registry(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    asset_class: Optional[str] = None,
    limit: Optional[int] = None,
) -> list[RegistryRow]:
    async with session_factory() as session:
        stmt = select(InstrumentRegistry).where(InstrumentRegistry.retired_at.is_(None))
        if asset_class:
            stmt = stmt.where(InstrumentRegistry.asset_class == asset_class)
        if limit:
            stmt = stmt.limit(int(limit))
        rows = await session.execute(stmt)
        out: list[RegistryRow] = []
        for r in rows.scalars().all():
            out.append(
                RegistryRow(
                    canonical_symbol=r.canonical_symbol,
                    display_name=r.display_name,
                    asset_class=r.asset_class,
                    region=r.region,
                    exchange=r.exchange,
                    currency=r.currency,
                    sector=r.sector,
                    industry=r.industry,
                    isin=r.isin,
                    figi=r.figi,
                    first_seen_at=r.first_seen_at,
                    last_seen_at=r.last_seen_at,
                    last_refreshed_at=r.last_refreshed_at,
                    retired_at=r.retired_at,
                    metadata=dict(r.metadata_ or {}),
                )
            )
    return out


async def list_broker_availability(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    broker: str,
    statuses: Optional[Sequence[str]] = None,
) -> list[AvailabilityRow]:
    async with session_factory() as session:
        stmt = select(InstrumentBrokerAvailability).where(
            InstrumentBrokerAvailability.broker == broker
        )
        if statuses:
            stmt = stmt.where(InstrumentBrokerAvailability.status.in_(list(statuses)))
        rows = await session.execute(stmt)
        out: list[AvailabilityRow] = []
        for r in rows.scalars().all():
            out.append(
                AvailabilityRow(
                    canonical_symbol=r.canonical_symbol,
                    broker=r.broker,
                    broker_symbol=r.broker_symbol,
                    status=r.status,
                    last_checked_at=r.last_checked_at,
                    last_available_at=r.last_available_at,
                    last_error=r.last_error,
                    qualification_payload=dict(r.qualification_payload or {})
                    if r.qualification_payload
                    else None,
                )
            )
    return out


async def list_recent_source_runs(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    limit_per_source: int = 1,
) -> dict[str, list[dict[str, Any]]]:
    """Return last-N runs per source. Used by the API health view."""
    async with session_factory() as session:
        rows = await session.execute(
            select(InstrumentSourceRun).order_by(
                InstrumentSourceRun.source_id, InstrumentSourceRun.started_at.desc()
            )
        )
        out: dict[str, list[dict[str, Any]]] = {}
        for r in rows.scalars().all():
            bucket = out.setdefault(r.source_id, [])
            if len(bucket) >= max(1, int(limit_per_source)):
                continue
            bucket.append(
                {
                    "id": r.id,
                    "source_id": r.source_id,
                    "started_at": r.started_at.isoformat() if r.started_at else None,
                    "finished_at": r.finished_at.isoformat() if r.finished_at else None,
                    "status": r.status,
                    "rows_added": r.rows_added,
                    "rows_updated": r.rows_updated,
                    "rows_missing": r.rows_missing,
                    "notes": r.notes,
                    "error": r.error,
                }
            )
    return out


async def registry_summary(
    session_factory: async_sessionmaker[AsyncSession],
) -> dict[str, Any]:
    """Counts by asset class / region / source / broker for diagnostics endpoint."""
    from sqlalchemy import func as sa_func  # local import to keep top imports lean

    async with session_factory() as session:
        active = await session.execute(
            select(sa_func.count())
            .select_from(InstrumentRegistry)
            .where(InstrumentRegistry.retired_at.is_(None))
        )
        retired = await session.execute(
            select(sa_func.count())
            .select_from(InstrumentRegistry)
            .where(InstrumentRegistry.retired_at.isnot(None))
        )
        by_asset = await session.execute(
            select(InstrumentRegistry.asset_class, sa_func.count())
            .where(InstrumentRegistry.retired_at.is_(None))
            .group_by(InstrumentRegistry.asset_class)
        )
        by_region = await session.execute(
            select(InstrumentRegistry.region, sa_func.count())
            .where(InstrumentRegistry.retired_at.is_(None))
            .group_by(InstrumentRegistry.region)
        )
        by_source = await session.execute(
            select(InstrumentSourceMembership.source_id, sa_func.count())
            .group_by(InstrumentSourceMembership.source_id)
        )
        by_broker = await session.execute(
            select(
                InstrumentBrokerAvailability.broker,
                InstrumentBrokerAvailability.status,
                sa_func.count(),
            ).group_by(
                InstrumentBrokerAvailability.broker,
                InstrumentBrokerAvailability.status,
            )
        )
        broker_buckets: dict[str, dict[str, int]] = {}
        for broker, status, count in by_broker.all():
            broker_buckets.setdefault(broker, {})[status] = int(count)
        return {
            "active": int(active.scalar() or 0),
            "retired": int(retired.scalar() or 0),
            "by_asset_class": {ac or "unknown": int(c) for ac, c in by_asset.all()},
            "by_region": {r or "unknown": int(c) for r, c in by_region.all()},
            "by_source": {src or "unknown": int(c) for src, c in by_source.all()},
            "by_broker_status": broker_buckets,
        }


async def get_registry_row(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    canonical_symbol: str,
) -> Optional[RegistryRow]:
    async with session_factory() as session:
        row = await session.get(InstrumentRegistry, canonical_symbol)
        if row is None:
            return None
        return RegistryRow(
            canonical_symbol=row.canonical_symbol,
            display_name=row.display_name,
            asset_class=row.asset_class,
            region=row.region,
            exchange=row.exchange,
            currency=row.currency,
            sector=row.sector,
            industry=row.industry,
            isin=row.isin,
            figi=row.figi,
            first_seen_at=row.first_seen_at,
            last_seen_at=row.last_seen_at,
            last_refreshed_at=row.last_refreshed_at,
            retired_at=row.retired_at,
            metadata=dict(row.metadata_ or {}),
        )


async def list_source_membership(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    canonical_symbol: str,
) -> list[dict[str, Any]]:
    async with session_factory() as session:
        rows = await session.execute(
            select(InstrumentSourceMembership).where(
                InstrumentSourceMembership.canonical_symbol == canonical_symbol
            )
        )
        out: list[dict[str, Any]] = []
        for r in rows.scalars().all():
            out.append(
                {
                    "source_id": r.source_id,
                    "source_version": r.source_version,
                    "external_id": r.external_id,
                    "last_seen_at": r.last_seen_at.isoformat() if r.last_seen_at else None,
                    "consecutive_miss_count": int(r.consecutive_miss_count or 0),
                    "metadata": dict(r.metadata_ or {}),
                }
            )
    return out
