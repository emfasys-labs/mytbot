"""CLI — build / refresh the D116 instrument registry.

Operator entry point that triggers a refresh on demand without going
through the orchestrator scheduler. Supports dry runs (no DB writes) and
selecting a subset of source ids or running only the availability
resolver against a list of brokers.

Examples
--------

Run every source against the live database::

    python scripts/build_instrument_registry.py --sources=all

Dry run: fetch + parse only, no DB writes::

    python scripts/build_instrument_registry.py --sources=all --dry-run

Run a specific source id only::

    python scripts/build_instrument_registry.py --sources=wikipedia.sp500,ishares.IVV

Only resolve broker availability (skip constituent fetch)::

    python scripts/build_instrument_registry.py --availability-only --brokers=alpaca,kraken
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from loguru import logger

# Allow ``python scripts/build_instrument_registry.py`` from the repo root.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from instruments.builder import (
    BuilderConfig,
    load_config,
    run_availability,
    run_refresh,
)
from storage.db import init_async_database


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Build / refresh the instrument registry (D116).")
    p.add_argument(
        "--sources",
        default="all",
        help="Comma-separated source ids (e.g. wikipedia.sp500,ishares.IVV) or 'all'.",
    )
    p.add_argument(
        "--brokers",
        default="",
        help="Optional comma-separated broker list for availability resolution (default: all connected).",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch + parse only; no DB writes.",
    )
    p.add_argument(
        "--availability-only",
        action="store_true",
        help="Skip constituent fetch; only run the per-broker availability resolver.",
    )
    p.add_argument(
        "--enrich-openfigi",
        action="store_true",
        help="Also run OpenFIGI enrichment after constituent refresh.",
    )
    p.add_argument(
        "--config",
        default=None,
        help="Optional path to a config/instrument_registry.yaml override.",
    )
    p.add_argument(
        "--report",
        default=None,
        help="Optional path to write a JSON report.",
    )
    return p.parse_args(argv)


async def _broker_manager_or_none(no_db: bool):
    """Construct a stand-alone ``BrokerManager`` for CLI runs.

    The orchestrator owns broker discovery during normal operation; for
    the CLI we spin one up directly so broker-catalog sources and the
    availability resolver work. Disconnects are best-effort.
    """
    if no_db:
        return None
    try:
        from system.broker_manager import BrokerManager

        bm = BrokerManager(paper_mode=True)
        await bm.discover_and_connect()
        return bm
    except Exception as exc:  # noqa: BLE001
        logger.warning("script | broker manager init failed (non-fatal): {}", exc)
        return None


async def amain(args: argparse.Namespace) -> int:
    cfg = load_config(args.config) if args.config else load_config()
    if not cfg.enabled:
        logger.warning("script | instrument registry is disabled in config; aborting")
        return 2

    select = None
    if args.sources and args.sources.strip().lower() != "all":
        select = [s.strip() for s in args.sources.split(",") if s.strip()]

    if args.dry_run:
        engine = None
        session_factory = None
    else:
        engine, session_factory = await init_async_database()
        if session_factory is None:
            logger.warning("script | database unavailable; switching to dry-run")
            args.dry_run = True

    broker_manager = await _broker_manager_or_none(no_db=args.dry_run)

    report: dict[str, object] = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "dry_run": bool(args.dry_run),
        "select": select or "all",
    }

    if not args.availability_only:
        if args.dry_run:
            logger.info("script | dry-run constituents refresh")
            refresh_report = await run_refresh(
                session_factory,  # type: ignore[arg-type]
                config=cfg,
                broker_manager=broker_manager,
                select=select,
                dry_run=True,
                enrich_openfigi=False,
            )
        else:
            refresh_report = await run_refresh(
                session_factory,  # type: ignore[arg-type]
                config=cfg,
                broker_manager=broker_manager,
                select=select,
                dry_run=False,
                enrich_openfigi=bool(args.enrich_openfigi),
            )
        report["refresh"] = {
            "sources": [
                {
                    "source_id": r.source_id,
                    "status": r.status,
                    "rows_added": r.rows_added,
                    "rows_updated": r.rows_updated,
                    "rows_missing": r.rows_missing,
                    "contributions": r.contributions,
                    "error": r.error,
                    "notes": r.notes,
                }
                for r in refresh_report.sources
            ],
            "retired": refresh_report.retired,
            "summary": refresh_report.summary,
        }

    if (args.availability_only or not args.dry_run) and broker_manager is not None:
        only_brokers = [b.strip() for b in (args.brokers or "").split(",") if b.strip()]
        avail = await run_availability(
            session_factory,  # type: ignore[arg-type]
            broker_manager=broker_manager,
            config=cfg,
            only_brokers=only_brokers or None,
        )
        report["availability"] = [
            {
                "broker": r.broker,
                "rows": r.rows,
                "available": r.available,
                "unavailable": r.unavailable,
                "requires_qualification": r.requires_qualification,
                "blocked": r.blocked,
                "fetched_catalog": r.fetched_catalog,
                "error": r.error,
            }
            for r in avail
        ]

    if broker_manager is not None:
        try:
            await broker_manager.disconnect_all()
        except Exception as exc:  # noqa: BLE001
            logger.debug("script | broker disconnect error: {}", exc)
    if engine is not None:
        try:
            await engine.dispose()
        except Exception:  # noqa: BLE001
            pass

    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    out = json.dumps(report, indent=2, default=str)
    print(out)
    if args.report:
        try:
            Path(args.report).parent.mkdir(parents=True, exist_ok=True)
            Path(args.report).write_text(out, encoding="utf-8")
        except OSError as exc:
            logger.warning("script | could not write report: {}", exc)
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    return asyncio.run(amain(args))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
