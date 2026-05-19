"""Broker-catalog source.

Each connected broker's ``BrokerAdapter.get_supported_symbols()`` becomes
its own source. This achieves two goals:

1. Crypto pairs (mainly from Kraken/Binance/Bybit) are added to the
   registry without us hand-maintaining them.
2. The registry contains everything *any* connected broker can actually
   trade today — when a new broker is connected later, its catalog is
   simply a new source contribution; nothing else needs to change.

The same broker adapter is later used by ``instruments.availability`` to
resolve per-broker availability. This source only contributes to the
registry; the availability resolver is separate.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Optional

from loguru import logger

from instruments.canonical import to_canonical
from instruments.registry import SourceContribution, coerce_contribution
from instruments.sources.base import (
    SourceContext,
    SourceFetchError,
    SourceFetchResult,
)


@dataclass
class BrokerCatalogSource:
    """Wraps a single broker's ``get_supported_symbols()`` into a source.

    Failure mode: a broker that times out / is unavailable yields an empty
    contribution list with ``partial=True``; the builder treats this as
    'this run did not see the broker' rather than 'symbols disappeared',
    so the retire policy is not falsely triggered.
    """

    broker_name: str
    adapter: Any
    timeout_sec: float = 20.0
    source_version: str = "broker_catalog.v1"

    @property
    def source_id(self) -> str:
        return f"broker_catalog.{self.broker_name.lower()}"

    @property
    def cadence_sec(self) -> int:
        return 3_600

    async def fetch(self, ctx: SourceContext) -> SourceFetchResult:
        if self.adapter is None:
            raise SourceFetchError(
                f"broker_catalog.{self.broker_name}: adapter not connected"
            )
        try:
            raw_symbols = await asyncio.wait_for(
                self.adapter.get_supported_symbols(), timeout=self.timeout_sec
            )
        except Exception as exc:  # noqa: BLE001
            raise SourceFetchError(
                f"broker_catalog.{self.broker_name}: get_supported_symbols failed: {exc}"
            ) from exc

        contributions: list[SourceContribution] = []
        seen: set[str] = set()
        for raw in raw_symbols or []:
            parsed = to_canonical(str(raw), broker=self.broker_name)
            if parsed is None:
                continue
            if parsed.symbol in seen:
                continue
            seen.add(parsed.symbol)
            contrib = coerce_contribution(
                parsed.symbol,
                asset_class_hint=parsed.asset_class,
                region_hint=parsed.region,
                exchange=parsed.exchange,
                external_id=str(raw),
                metadata={
                    "source_id": self.source_id,
                    "broker": self.broker_name,
                    "broker_symbol": str(raw),
                },
            )
            if contrib is not None:
                contributions.append(contrib)

        logger.info(
            "instruments.broker_catalog | {} contributed rows={}",
            self.broker_name,
            len(contributions),
        )
        return SourceFetchResult(
            source_id=self.source_id,
            source_version=self.source_version,
            contributions=contributions,
            fetched_at=ctx.started_at,
            partial=False,
        )


def get_broker_catalog_sources(
    broker_manager: Any,
    *,
    excluded_brokers: Optional[Iterable[str]] = None,
) -> list[BrokerCatalogSource]:
    """Materialise broker-catalog sources from a live ``BrokerManager``."""
    if broker_manager is None or not getattr(broker_manager, "adapters", None):
        return []
    excluded = {str(b).lower() for b in (excluded_brokers or ())}
    out: list[BrokerCatalogSource] = []
    for name, adapter in broker_manager.adapters.items():
        if name.lower() in excluded:
            continue
        out.append(BrokerCatalogSource(broker_name=name, adapter=adapter))
    return out
