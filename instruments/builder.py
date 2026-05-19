"""Builder for the D116 instrument registry.

Orchestrates source adapters, registry upserts, retire policy, audit
recording, and per-broker availability resolution. Each source runs in
its own ``try/except`` block so a single failure cannot poison the run.

Public entry points:

- :func:`run_refresh` — full multi-source refresh.
- :func:`run_availability` — per-broker availability resolution.
- :func:`load_config` — load + validate ``config/instrument_registry.yaml``.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

import yaml
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from instruments.availability import (
    AvailabilityResolution,
    AvailabilityResolverConfig,
    resolve_all_brokers,
    resolve_broker_availability,
)
from instruments.registry import (
    SourceContribution,
    UpsertSummary,
    apply_retire_policy,
    list_active_registry,
    record_source_run,
    registry_summary,
    upsert_contributions,
)
from instruments.sources.base import (
    Source,
    SourceContext,
    SourceFetchError,
    SourceFetchResult,
)


DEFAULT_CONFIG_PATH = Path("config/instrument_registry.yaml")


@dataclass(frozen=True)
class BuilderConfig:
    enabled: bool = True
    retire_policy: Mapping[str, int] = field(default_factory=lambda: {
        "min_consecutive_misses": 5,
        "min_sources_missing": 2,
    })
    overrides: Mapping[str, Sequence[str]] = field(default_factory=lambda: {
        "pinned": (),
        "excluded": (),
    })
    sources_enabled: Mapping[str, bool] = field(default_factory=dict)
    source_ids_enabled: Mapping[str, Sequence[str]] = field(default_factory=dict)
    openfigi_api_key_env: str = "OPENFIGI_API_KEY"
    openfigi_max_symbols: int = 5_000
    broker_catalog_excluded: tuple[str, ...] = ()
    availability_timeout_sec: float = 20.0
    ibkr_supported_symbols_use_registry: bool = False


@dataclass(frozen=True)
class SourceRunResult:
    source_id: str
    status: str
    rows_added: int
    rows_updated: int
    rows_missing: int
    contributions: int
    started_at: datetime
    finished_at: datetime
    error: Optional[str] = None
    notes: Optional[str] = None


@dataclass(frozen=True)
class RefreshReport:
    started_at: datetime
    finished_at: datetime
    sources: tuple[SourceRunResult, ...]
    retired: int
    summary: Mapping[str, Any]

    @property
    def total_added(self) -> int:
        return sum(r.rows_added for r in self.sources)

    @property
    def total_updated(self) -> int:
        return sum(r.rows_updated for r in self.sources)

    @property
    def total_missing(self) -> int:
        return sum(r.rows_missing for r in self.sources)


def load_config(path: str | Path | None = None) -> BuilderConfig:
    """Load and validate the instrument-registry config.

    Returns sane defaults when the file is missing or malformed; never raises.
    """
    p = Path(path) if path is not None else DEFAULT_CONFIG_PATH
    raw: Any = {}
    if p.is_file():
        try:
            raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as exc:
            logger.warning("instruments.builder | config load failed (using defaults): {}", exc)
            raw = {}
    if not isinstance(raw, dict):
        raw = {}

    retire_raw = raw.get("retire") or {}
    retire_policy = {
        "min_consecutive_misses": int(retire_raw.get("min_consecutive_misses", 5) or 5),
        "min_sources_missing": int(retire_raw.get("min_sources_missing", 2) or 2),
    }
    overrides_raw = raw.get("overrides") or {}
    overrides = {
        "pinned": tuple(s for s in (overrides_raw.get("pinned") or []) if isinstance(s, str)),
        "excluded": tuple(s for s in (overrides_raw.get("excluded") or []) if isinstance(s, str)),
    }
    sources_raw = raw.get("sources") or {}
    sources_enabled = {
        name: bool((data or {}).get("enabled", True))
        for name, data in sources_raw.items()
        if isinstance(data, dict)
    }
    source_ids_enabled = {
        name: tuple(
            (data or {}).get("enabled_ids") or (data or {}).get("indices") or (data or {}).get("funds") or []
        )
        for name, data in sources_raw.items()
        if isinstance(data, dict)
    }
    openfigi_raw = sources_raw.get("openfigi") or {}
    return BuilderConfig(
        enabled=bool(raw.get("enabled", True)),
        retire_policy=retire_policy,
        overrides=overrides,
        sources_enabled=sources_enabled,
        source_ids_enabled=source_ids_enabled,
        openfigi_api_key_env=str(openfigi_raw.get("api_key_env") or "OPENFIGI_API_KEY"),
        openfigi_max_symbols=int(openfigi_raw.get("max_symbols") or 5_000),
        broker_catalog_excluded=tuple(
            (sources_raw.get("broker_catalog") or {}).get("excluded_brokers") or ()
        ),
        availability_timeout_sec=float(
            (raw.get("availability") or {}).get("timeout_sec") or 20.0
        ),
        ibkr_supported_symbols_use_registry=bool(
            raw.get("ibkr_supported_symbols_use_registry", False)
        ),
    )


def _filter_id_list(raw: Any) -> list[str]:
    """Normalise ``enabled_ids`` style lists; tolerate dict-of-recipes shape."""
    if not raw:
        return []
    if isinstance(raw, list):
        return [str(s) for s in raw if isinstance(s, str) and s.strip()]
    if isinstance(raw, dict):
        return [str(k) for k in raw.keys() if isinstance(k, str)]
    return []


def build_sources(
    config: BuilderConfig,
    *,
    broker_manager: Any = None,
    select: Optional[Iterable[str]] = None,
) -> list[Source]:
    """Instantiate enabled source adapters.

    ``select``: optional set of glob-free source-id prefixes (e.g. ``wikipedia.sp500``,
    ``ishares.IVV``, ``broker_catalog.alpaca``); when present, only sources whose
    ``source_id`` appears in the set are returned.
    """
    selected: Optional[set[str]] = {s.strip() for s in select if s.strip()} if select else None

    def _maybe_keep(source_id: str) -> bool:
        return selected is None or source_id in selected

    sources: list[Source] = []

    if config.sources_enabled.get("wikipedia", True):
        from instruments.sources.wikipedia import (
            WIKIPEDIA_INDEX_REGISTRY,
            WikipediaSource,
            get_wikipedia_sources,
        )

        enabled_ids = _filter_id_list(config.source_ids_enabled.get("wikipedia"))
        if enabled_ids:
            for src in get_wikipedia_sources(enabled_source_ids=enabled_ids):
                if _maybe_keep(src.source_id):
                    sources.append(src)
        else:
            for entry in WIKIPEDIA_INDEX_REGISTRY:
                if _maybe_keep(entry.source_id):
                    sources.append(WikipediaSource(entry))

    if config.sources_enabled.get("ishares", True):
        from instruments.sources.ishares import (
            ISHARES_FUND_REGISTRY,
            IsharesSource,
            get_ishares_sources,
        )

        enabled_ids = _filter_id_list(config.source_ids_enabled.get("ishares"))
        if enabled_ids:
            for src in get_ishares_sources(enabled_source_ids=enabled_ids):
                if _maybe_keep(src.source_id):
                    sources.append(src)
        else:
            for entry in ISHARES_FUND_REGISTRY:
                if _maybe_keep(entry.source_id):
                    sources.append(IsharesSource(entry))

    if config.sources_enabled.get("static_fx", True):
        from instruments.sources.static_fx import StaticFxSource

        if _maybe_keep("static.fx"):
            sources.append(StaticFxSource())

    if config.sources_enabled.get("static_futures", True):
        from instruments.sources.static_futures import StaticFuturesSource

        if _maybe_keep("static.futures"):
            sources.append(StaticFuturesSource())

    if config.sources_enabled.get("broker_catalog", True) and broker_manager is not None:
        from instruments.sources.broker_catalog import get_broker_catalog_sources

        for src in get_broker_catalog_sources(
            broker_manager,
            excluded_brokers=config.broker_catalog_excluded,
        ):
            if _maybe_keep(src.source_id):
                sources.append(src)

    return sources


async def _run_one_source(
    session_factory: async_sessionmaker[AsyncSession],
    source: Source,
    *,
    ctx: SourceContext,
    retire_policy: Mapping[str, int],
    dry_run: bool,
) -> SourceRunResult:
    started_at = datetime.now(timezone.utc)
    try:
        result: SourceFetchResult = await source.fetch(ctx)
    except SourceFetchError as exc:
        finished_at = datetime.now(timezone.utc)
        logger.warning("instruments.builder | source={} failed: {}", source.source_id, exc)
        if not dry_run:
            try:
                await record_source_run(
                    session_factory,
                    source_id=source.source_id,
                    started_at=started_at,
                    finished_at=finished_at,
                    status="failed",
                    rows_added=0,
                    rows_updated=0,
                    rows_missing=0,
                    notes=None,
                    error=str(exc),
                )
            except Exception as audit_exc:  # noqa: BLE001
                logger.debug("instruments.builder | audit write failed: {}", audit_exc)
        return SourceRunResult(
            source_id=source.source_id,
            status="failed",
            rows_added=0,
            rows_updated=0,
            rows_missing=0,
            contributions=0,
            started_at=started_at,
            finished_at=finished_at,
            error=str(exc),
        )
    except Exception as exc:  # noqa: BLE001
        finished_at = datetime.now(timezone.utc)
        logger.exception("instruments.builder | unexpected error in {}: {}", source.source_id, exc)
        return SourceRunResult(
            source_id=source.source_id,
            status="failed",
            rows_added=0,
            rows_updated=0,
            rows_missing=0,
            contributions=0,
            started_at=started_at,
            finished_at=finished_at,
            error=str(exc),
        )

    summary: UpsertSummary
    if dry_run:
        summary = UpsertSummary(
            rows_added=len(result.contributions),
            rows_updated=0,
            rows_missing=0,
            contributions=len(result.contributions),
            skipped=0,
        )
    else:
        summary = await upsert_contributions(
            session_factory,
            source_id=source.source_id,
            source_version=result.source_version,
            contributions=result.contributions,
            retire_policy=retire_policy,
        )
    finished_at = datetime.now(timezone.utc)
    status = "success" if not result.partial else "partial"
    if not dry_run:
        try:
            await record_source_run(
                session_factory,
                source_id=source.source_id,
                started_at=started_at,
                finished_at=finished_at,
                status=status,
                rows_added=summary.rows_added,
                rows_updated=summary.rows_updated,
                rows_missing=summary.rows_missing,
                notes=result.notes,
                error=None,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("instruments.builder | audit write failed: {}", exc)
    return SourceRunResult(
        source_id=source.source_id,
        status=status,
        rows_added=summary.rows_added,
        rows_updated=summary.rows_updated,
        rows_missing=summary.rows_missing,
        contributions=summary.contributions,
        started_at=started_at,
        finished_at=finished_at,
        error=None,
        notes=result.notes,
    )


async def run_refresh(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    config: Optional[BuilderConfig] = None,
    broker_manager: Any = None,
    select: Optional[Iterable[str]] = None,
    dry_run: bool = False,
    enrich_openfigi: bool = False,
) -> RefreshReport:
    cfg = config or load_config()
    started_at = datetime.now(timezone.utc)
    ctx = SourceContext(started_at=started_at, config={})
    sources = build_sources(cfg, broker_manager=broker_manager, select=select)
    if not sources:
        logger.info("instruments.builder | no sources enabled — nothing to do")
        return RefreshReport(
            started_at=started_at,
            finished_at=datetime.now(timezone.utc),
            sources=(),
            retired=0,
            summary={},
        )

    results: list[SourceRunResult] = []
    for src in sources:
        result = await _run_one_source(
            session_factory,
            src,
            ctx=ctx,
            retire_policy=cfg.retire_policy,
            dry_run=dry_run,
        )
        results.append(result)

    if enrich_openfigi and cfg.sources_enabled.get("openfigi", True) and not dry_run:
        try:
            from instruments.sources.openfigi import OpenFIGISource

            seed = await list_active_registry(session_factory)
            ofigi = OpenFIGISource(
                seed=tuple(seed[: cfg.openfigi_max_symbols]),
                api_key_env=cfg.openfigi_api_key_env,
                max_symbols=cfg.openfigi_max_symbols,
            )
            results.append(
                await _run_one_source(
                    session_factory,
                    ofigi,
                    ctx=ctx,
                    retire_policy=cfg.retire_policy,
                    dry_run=False,
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("instruments.builder | OpenFIGI enrichment skipped: {}", exc)

    retired = 0
    if not dry_run:
        try:
            retired = await apply_retire_policy(session_factory, retire_policy=cfg.retire_policy)
        except Exception as exc:  # noqa: BLE001
            logger.debug("instruments.builder | retire policy failed (non-fatal): {}", exc)
        try:
            summary = await registry_summary(session_factory)
        except Exception as exc:  # noqa: BLE001
            logger.debug("instruments.builder | summary load failed: {}", exc)
            summary = {}
    else:
        summary = {}

    finished_at = datetime.now(timezone.utc)
    report = RefreshReport(
        started_at=started_at,
        finished_at=finished_at,
        sources=tuple(results),
        retired=retired,
        summary=summary,
    )
    logger.info(
        "instruments.builder | refresh complete sources={} added={} updated={} missing={} retired={}",
        len(results),
        report.total_added,
        report.total_updated,
        report.total_missing,
        retired,
    )
    return report


async def run_availability(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    broker_manager: Any,
    config: Optional[BuilderConfig] = None,
    only_brokers: Optional[Iterable[str]] = None,
) -> list[AvailabilityResolution]:
    cfg = config or load_config()
    if broker_manager is None or not getattr(broker_manager, "adapters", None):
        return []
    only_set = {b.strip().lower() for b in (only_brokers or [])}
    resolver_cfg = AvailabilityResolverConfig(
        blocked=frozenset(cfg.overrides.get("excluded") or ()),
        pinned=frozenset(cfg.overrides.get("pinned") or ()),
        timeout_sec=cfg.availability_timeout_sec,
    )
    out: list[AvailabilityResolution] = []
    for name, adapter in broker_manager.adapters.items():
        if only_set and name.lower() not in only_set:
            continue
        try:
            res = await resolve_broker_availability(
                session_factory,
                broker=name,
                adapter=adapter,
                config=resolver_cfg,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "instruments.builder | availability failed for {} (non-fatal): {}", name, exc
            )
            continue
        out.append(res)
    return out
