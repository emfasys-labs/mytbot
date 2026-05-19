"""Per-broker availability resolver for the D116 instrument registry.

The resolver is intentionally lightweight and read-mostly:

1. Snapshot the active registry.
2. Pull the broker's current catalog (or, for IBKR, the curated seed
   plus the persisted qualification cache).
3. Translate canonical → broker-native via :mod:`instruments.canonical`.
4. Mark each (canonical, broker) row as ``available``, ``unavailable``,
   ``requires_qualification`` (IBKR-only when no qualification record
   exists yet), or ``blocked`` (operator override).
5. Upsert through :mod:`instruments.registry.upsert_broker_availability`.

This module never places orders. IBKR contract qualification still
happens inside ``brokers/ibkr/adapter.py`` before ``place_order`` —
this resolver only marks symbols as ``requires_qualification`` so that
the discovery layer can include them in the IBKR seed.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Optional, Sequence

from loguru import logger
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from instruments.canonical import canonical_to_broker, to_canonical
from instruments.registry import (
    AvailabilityRow,
    RegistryRow,
    list_active_registry,
    upsert_broker_availability,
)


@dataclass(frozen=True)
class AvailabilityResolverConfig:
    blocked: frozenset[str] = frozenset()
    pinned: frozenset[str] = frozenset()
    timeout_sec: float = 20.0


@dataclass(frozen=True)
class AvailabilityResolution:
    broker: str
    rows: int
    available: int
    unavailable: int
    requires_qualification: int
    blocked: int
    fetched_catalog: bool
    error: Optional[str] = None


async def _broker_catalog(adapter: Any, timeout_sec: float) -> Optional[set[str]]:
    if adapter is None:
        return None
    try:
        symbols = await asyncio.wait_for(adapter.get_supported_symbols(), timeout=timeout_sec)
    except Exception as exc:  # noqa: BLE001
        logger.debug("instruments.availability | catalog fetch failed: {}", exc)
        return None
    out: set[str] = set()
    for s in symbols or []:
        sx = str(s).strip().upper()
        if sx:
            out.add(sx)
    return out


def _ibkr_qualified_set() -> set[str]:
    """Read the existing IBKR qualification cache without forcing a circular import.

    Returns the set of broker-side symbols whose qualification status is
    confirmed in :class:`brokers.ibkr.qualification.IBKRQualificationCache`.
    Defensive: any failure returns an empty set so availability resolution
    can still proceed.
    """
    try:
        from brokers.ibkr.qualification import IBKRQualificationCache
    except ImportError:
        return set()
    try:
        cache = IBKRQualificationCache()
    except Exception:  # noqa: BLE001
        return set()
    out: set[str] = set()
    for rec in cache.all():
        if not rec.is_qualified():
            continue
        sym = rec.broker_symbol or rec.symbol
        if sym:
            out.add(str(sym).upper())
    return out


def _resolve_one(
    row: RegistryRow,
    *,
    broker: str,
    catalog: Optional[set[str]],
    ibkr_qualified: set[str],
    config: AvailabilityResolverConfig,
) -> AvailabilityRow:
    sym = row.canonical_symbol
    broker_sym = canonical_to_broker(sym, broker)
    now = datetime.now(timezone.utc)
    if sym in config.blocked:
        return AvailabilityRow(
            canonical_symbol=sym,
            broker=broker,
            broker_symbol=broker_sym,
            status="blocked",
            last_checked_at=now,
            last_available_at=None,
            last_error=None,
        )
    if broker_sym is None:
        return AvailabilityRow(
            canonical_symbol=sym,
            broker=broker,
            broker_symbol=None,
            status="unavailable",
            last_checked_at=now,
            last_available_at=None,
            last_error="no broker-side translation",
        )

    # IBKR special case: catalog reflects the curated seed, but anything that
    # qualifies via contract qualification is also tradable. If the symbol
    # is in either set, mark available; otherwise mark requires_qualification.
    if broker.lower() == "ibkr":
        if catalog and broker_sym.upper() in {s.upper() for s in catalog}:
            return AvailabilityRow(
                canonical_symbol=sym,
                broker=broker,
                broker_symbol=broker_sym,
                status="available",
                last_checked_at=now,
                last_available_at=now,
                last_error=None,
            )
        if broker_sym.upper() in ibkr_qualified:
            return AvailabilityRow(
                canonical_symbol=sym,
                broker=broker,
                broker_symbol=broker_sym,
                status="available",
                last_checked_at=now,
                last_available_at=now,
                last_error=None,
            )
        return AvailabilityRow(
            canonical_symbol=sym,
            broker=broker,
            broker_symbol=broker_sym,
            status="requires_qualification",
            last_checked_at=now,
            last_available_at=None,
            last_error=None,
        )

    if catalog is None:
        return AvailabilityRow(
            canonical_symbol=sym,
            broker=broker,
            broker_symbol=broker_sym,
            status="unknown",
            last_checked_at=now,
            last_available_at=None,
            last_error="catalog unavailable",
        )

    if broker_sym.upper() in {s.upper() for s in catalog}:
        return AvailabilityRow(
            canonical_symbol=sym,
            broker=broker,
            broker_symbol=broker_sym,
            status="available",
            last_checked_at=now,
            last_available_at=now,
            last_error=None,
        )
    return AvailabilityRow(
        canonical_symbol=sym,
        broker=broker,
        broker_symbol=broker_sym,
        status="unavailable",
        last_checked_at=now,
        last_available_at=None,
        last_error=None,
    )


async def resolve_broker_availability(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    broker: str,
    adapter: Any,
    config: Optional[AvailabilityResolverConfig] = None,
    scope: Optional[Sequence[str]] = None,
    asset_class_filter: Optional[str] = None,
) -> AvailabilityResolution:
    """Resolve and persist availability for one broker.

    ``scope`` lets the caller restrict resolution to a subset of canonical
    symbols (e.g. for on-demand qualification of a specific row). When
    ``adapter`` is ``None`` (broker disconnected), all in-scope rows are
    marked ``unknown`` with ``last_error='catalog unavailable'``.
    """
    cfg = config or AvailabilityResolverConfig()
    registry = await list_active_registry(session_factory, asset_class=asset_class_filter)
    if scope is not None:
        scope_set = {s.upper() for s in scope}
        registry = [r for r in registry if r.canonical_symbol in scope_set]
    catalog = await _broker_catalog(adapter, cfg.timeout_sec)
    fetched = catalog is not None
    ibkr_qualified = _ibkr_qualified_set() if broker.lower() == "ibkr" else set()

    rows: list[AvailabilityRow] = []
    available = unavailable = req_qual = blocked = 0
    for r in registry:
        res = _resolve_one(
            r,
            broker=broker,
            catalog=catalog,
            ibkr_qualified=ibkr_qualified,
            config=cfg,
        )
        rows.append(res)
        if res.status == "available":
            available += 1
        elif res.status == "requires_qualification":
            req_qual += 1
        elif res.status == "blocked":
            blocked += 1
        else:
            unavailable += 1

    await upsert_broker_availability(session_factory, broker=broker, rows=rows)

    logger.info(
        "instruments.availability | {} resolved rows={} avail={} unavail={} req_qual={} blocked={} fetched_catalog={}",
        broker,
        len(rows),
        available,
        unavailable,
        req_qual,
        blocked,
        fetched,
    )
    return AvailabilityResolution(
        broker=broker,
        rows=len(rows),
        available=available,
        unavailable=unavailable,
        requires_qualification=req_qual,
        blocked=blocked,
        fetched_catalog=fetched,
    )


async def resolve_all_brokers(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    broker_manager: Any,
    config: Optional[AvailabilityResolverConfig] = None,
) -> list[AvailabilityResolution]:
    if broker_manager is None or not getattr(broker_manager, "adapters", None):
        return []
    out: list[AvailabilityResolution] = []
    for name, adapter in broker_manager.adapters.items():
        try:
            res = await resolve_broker_availability(
                session_factory,
                broker=name,
                adapter=adapter,
                config=config,
            )
            out.append(res)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "instruments.availability | broker={} failed (non-fatal): {}", name, exc
            )
            out.append(
                AvailabilityResolution(
                    broker=name,
                    rows=0,
                    available=0,
                    unavailable=0,
                    requires_qualification=0,
                    blocked=0,
                    fetched_catalog=False,
                    error=str(exc),
                )
            )
    return out
