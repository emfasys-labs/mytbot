"""Curated FX pair source.

FX pairs are a well-bounded universe (G10 majors + crosses + a handful of
EM majors). We do not crawl them from a third party because Wikipedia/iShares
do not publish a single authoritative list and the answer barely moves.
"""

from __future__ import annotations

from datetime import datetime
from typing import Iterable, Optional, Sequence

from instruments.registry import SourceContribution, coerce_contribution
from instruments.sources.base import (
    SourceContext,
    SourceFetchResult,
)


G10_MAJORS = ("USD", "EUR", "GBP", "JPY", "CHF", "AUD", "CAD", "NZD", "NOK", "SEK")
EM_MAJORS = ("CNH", "HKD", "SGD", "MXN", "ZAR", "PLN", "TRY", "INR", "BRL", "ILS")


def _fx_pairs() -> list[str]:
    pairs: list[str] = []
    seen: set[str] = set()
    # G10 vs USD (both orientations as the market quotes them)
    for ccy in G10_MAJORS:
        if ccy == "USD":
            continue
        if ccy in {"EUR", "GBP", "AUD", "NZD"}:
            pair = f"{ccy}USD=X"
        else:
            pair = f"USD{ccy}=X"
        if pair not in seen:
            seen.add(pair)
            pairs.append(pair)
    # All G10 crosses
    for i, a in enumerate(G10_MAJORS):
        for b in G10_MAJORS[i + 1 :]:
            if a == "USD" or b == "USD":
                continue
            pair = f"{a}{b}=X"
            if pair not in seen:
                seen.add(pair)
                pairs.append(pair)
    # USD vs EM majors
    for em in EM_MAJORS:
        pair = f"USD{em}=X"
        if pair not in seen:
            seen.add(pair)
            pairs.append(pair)
    return pairs


class StaticFxSource:
    """Curated FX pair source — fast, offline, deterministic."""

    source_id = "static.fx"
    source_version = "static_fx.v1"
    cadence_sec = 604_800  # weekly; the set rarely changes

    async def fetch(self, ctx: SourceContext) -> SourceFetchResult:
        contributions: list[SourceContribution] = []
        for pair in _fx_pairs():
            contrib = coerce_contribution(
                pair,
                asset_class_hint="fx",
                region_hint="Global",
                display_name=pair.replace("=X", ""),
                exchange="IDEALPRO",
                external_id=pair,
                metadata={"source_id": self.source_id},
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


def get_static_fx_sources(*, enabled: bool = True) -> list[StaticFxSource]:
    return [StaticFxSource()] if enabled else []
