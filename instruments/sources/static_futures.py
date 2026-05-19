"""Curated continuous-future source.

Mirrors the futures roots already in the data pipeline (ES, NQ, YM, ...) so
the registry has a canonical entry for each. ``FUTURES_EXECUTION_ENABLED``
remains the gate that decides whether the trading loop tries to route
orders for these.
"""

from __future__ import annotations

from datetime import datetime
from typing import Iterable, Optional

from instruments.registry import SourceContribution, coerce_contribution
from instruments.sources.base import (
    SourceContext,
    SourceFetchResult,
)


FUTURES_ROOTS: tuple[tuple[str, str, str], ...] = (
    ("ES", "E-mini S&P 500", "Equity index"),
    ("NQ", "E-mini Nasdaq 100", "Equity index"),
    ("YM", "E-mini Dow Jones", "Equity index"),
    ("RTY", "E-mini Russell 2000", "Equity index"),
    ("CL", "WTI Crude Oil", "Energy"),
    ("BZ", "Brent Crude", "Energy"),
    ("NG", "Henry Hub Natural Gas", "Energy"),
    ("GC", "Gold", "Metals"),
    ("SI", "Silver", "Metals"),
    ("HG", "Copper", "Metals"),
    ("PL", "Platinum", "Metals"),
    ("PA", "Palladium", "Metals"),
    ("ZN", "10y US Treasury", "Rates"),
    ("ZB", "30y US Treasury", "Rates"),
    ("ZF", "5y US Treasury", "Rates"),
    ("ZT", "2y US Treasury", "Rates"),
    ("ZC", "Corn", "Agriculture"),
    ("ZS", "Soybeans", "Agriculture"),
    ("ZW", "Wheat", "Agriculture"),
    ("KC", "Coffee", "Agriculture"),
    ("SB", "Sugar", "Agriculture"),
    ("CC", "Cocoa", "Agriculture"),
    ("CT", "Cotton", "Agriculture"),
)


class StaticFuturesSource:
    """Curated CME continuous-future source."""

    source_id = "static.futures"
    source_version = "static_futures.v1"
    cadence_sec = 604_800

    async def fetch(self, ctx: SourceContext) -> SourceFetchResult:
        contributions: list[SourceContribution] = []
        for root, name, group in FUTURES_ROOTS:
            sym = f"{root}=F"
            contrib = coerce_contribution(
                sym,
                asset_class_hint="future",
                region_hint="US",
                display_name=name,
                exchange="CME",
                external_id=root,
                sector=group,
                metadata={"source_id": self.source_id, "root": root, "group": group},
            )
            if contrib is not None:
                contributions.append(contrib)
        return SourceFetchResult(
            source_id=self.source_id,
            source_version=self.source_version,
            contributions=contributions,
            fetched_at=ctx.started_at,
            partial=False,
        )


def get_static_futures_sources(*, enabled: bool = True) -> list[StaticFuturesSource]:
    return [StaticFuturesSource()] if enabled else []
