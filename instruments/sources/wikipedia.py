"""Wikipedia index constituent source for the D116 instrument registry.

Each entry in :data:`WIKIPEDIA_INDEX_REGISTRY` is a small, declarative
description of where to find a constituent table on Wikipedia and how to
turn it into ``SourceContribution`` rows. The fetch flow is:

1. Polite HTTP GET with ETag/Last-Modified caching.
2. ``pandas.read_html`` extracts every table on the page.
3. The configured table index + symbol column is read.
4. Optional ``symbol_postprocess`` adds the exchange suffix needed by
   yfinance (e.g. ``.L`` for LSE, ``.DE`` for XETRA).
5. Rows are passed through ``instruments.registry.coerce_contribution``.

Sources here are constituent overlays — they do not list every share class
or every regional secondary listing, so the resulting universe is
representative of the index, not the global free float. That is the
intended scope; iShares ETF holdings complement this for breadth.
"""

from __future__ import annotations

import asyncio
import io
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Iterable, Mapping, Optional

import pandas as pd
from loguru import logger

from instruments.canonical import AssetClassHint
from instruments.registry import SourceContribution, coerce_contribution
from instruments.sources.base import (
    Source,
    SourceContext,
    SourceFetchError,
    SourceFetchResult,
)
from instruments.sources.http import polite_get


@dataclass(frozen=True)
class WikipediaIndexEntry:
    """Declarative recipe for one Wikipedia constituent table."""

    source_id: str
    url: str
    table_index: int
    symbol_column: str
    name_column: Optional[str] = None
    sector_column: Optional[str] = None
    industry_column: Optional[str] = None
    suffix: Optional[str] = None         # appended to bare ticker, e.g. ".L"
    region: str = "US"
    currency: str = "USD"
    asset_class: AssetClassHint = "equity"
    exchange: Optional[str] = None
    cadence_sec: int = 86_400
    fallback_table_indices: tuple[int, ...] = field(default_factory=tuple)


def _strip_footnote(value: object) -> str:
    s = str(value or "").strip()
    s = re.sub(r"\[\d+\]", "", s)            # remove [1], [2] footnotes
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def _normalise_ticker(raw: object, entry: WikipediaIndexEntry) -> Optional[str]:
    s = _strip_footnote(raw).upper().replace(" ", "")
    if not s or s in {"-", "—", "N/A", "NONE"}:
        return None
    # Wikipedia sometimes uses BRK.B / BF.B for US dual-class shares.
    if entry.suffix is None:
        s = s.replace(".B", "-B").replace(".A", "-A")
        return s if re.match(r"^[A-Z0-9\-]{1,12}$", s) else None
    # International: strip embedded suffix duplication then append canonical suffix.
    if s.endswith(entry.suffix):
        return s
    if "." in s and len(s.split(".")[-1]) <= 4:
        s = s.split(".")[0]
    return f"{s}{entry.suffix}"


def _select_table(tables: list[pd.DataFrame], entry: WikipediaIndexEntry) -> pd.DataFrame:
    configured = (entry.table_index, *entry.fallback_table_indices)
    candidates: Iterable[int] = (
        *configured,
        *(idx for idx in range(len(tables)) if idx not in configured),
    )
    last_exc: Optional[Exception] = None
    for idx in candidates:
        if 0 <= idx < len(tables):
            df = tables[idx]
            cols = {str(c).strip().lower() for c in df.columns}
            if entry.symbol_column.lower() in cols:
                return df
            last_exc = SourceFetchError(
                f"table_index={idx} missing column '{entry.symbol_column}' (cols={cols})"
            )
    raise SourceFetchError(
        f"no matching table for {entry.source_id} (tried {entry.table_index} + fallbacks)"
    ) from last_exc


def _col(df: pd.DataFrame, name: str) -> Optional[str]:
    for col in df.columns:
        if str(col).strip().lower() == name.strip().lower():
            return col
    return None


class WikipediaSource:
    """One Wikipedia constituent table source."""

    def __init__(self, entry: WikipediaIndexEntry) -> None:
        self._entry = entry
        self.source_id = entry.source_id
        self.source_version = "wikipedia.v1"
        self.cadence_sec = entry.cadence_sec

    async def fetch(self, ctx: SourceContext) -> SourceFetchResult:
        entry = self._entry
        resp = await polite_get(entry.url, headers={"Accept": "text/html"})
        try:
            tables = await asyncio.to_thread(pd.read_html, io.BytesIO(resp.content))
        except (ValueError, ImportError, AttributeError) as exc:
            raise SourceFetchError(f"pandas.read_html failed for {entry.url}: {exc}") from exc
        if not tables:
            raise SourceFetchError(f"no tables found at {entry.url}")
        df = _select_table(tables, entry)
        symbol_col = _col(df, entry.symbol_column)
        if symbol_col is None:
            raise SourceFetchError(
                f"symbol column '{entry.symbol_column}' missing in {entry.source_id}"
            )
        name_col = _col(df, entry.name_column) if entry.name_column else None
        sector_col = _col(df, entry.sector_column) if entry.sector_column else None
        industry_col = _col(df, entry.industry_column) if entry.industry_column else None

        contributions: list[SourceContribution] = []
        for _, row in df.iterrows():
            ticker_raw = row.get(symbol_col)
            ticker = _normalise_ticker(ticker_raw, entry)
            if not ticker:
                continue
            name_val = _strip_footnote(row.get(name_col)) if name_col else None
            sector_val = _strip_footnote(row.get(sector_col)) if sector_col else None
            industry_val = _strip_footnote(row.get(industry_col)) if industry_col else None
            contrib = coerce_contribution(
                ticker,
                asset_class_hint=entry.asset_class,
                region_hint=entry.region,
                display_name=name_val,
                exchange=entry.exchange,
                currency=entry.currency,
                sector=sector_val,
                industry=industry_val,
                external_id=ticker,
                metadata={"source_id": entry.source_id, "url": entry.url},
            )
            if contrib is not None:
                contributions.append(contrib)

        if not contributions:
            raise SourceFetchError(
                f"{entry.source_id} parsed {len(df)} rows but produced 0 contributions"
            )
        logger.info(
            "instruments.wikipedia | {} contributed rows={}",
            entry.source_id,
            len(contributions),
        )
        return SourceFetchResult(
            source_id=entry.source_id,
            source_version=self.source_version,
            contributions=contributions,
            fetched_at=datetime.fromtimestamp(ctx.started_at.timestamp(), tz=ctx.started_at.tzinfo),
            partial=False,
        )


WIKIPEDIA_INDEX_REGISTRY: tuple[WikipediaIndexEntry, ...] = (
    # Americas
    WikipediaIndexEntry(
        source_id="wikipedia.sp500",
        url="https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
        table_index=0,
        symbol_column="Symbol",
        name_column="Security",
        sector_column="GICS Sector",
        industry_column="GICS Sub-Industry",
        region="US",
        exchange="NYSE/Nasdaq",
    ),
    WikipediaIndexEntry(
        source_id="wikipedia.sp400",
        url="https://en.wikipedia.org/wiki/List_of_S%26P_400_companies",
        table_index=0,
        symbol_column="Symbol",
        name_column="Security",
        sector_column="GICS Sector",
        industry_column="GICS Sub-Industry",
        region="US",
        exchange="NYSE/Nasdaq",
    ),
    WikipediaIndexEntry(
        source_id="wikipedia.sp600",
        url="https://en.wikipedia.org/wiki/List_of_S%26P_600_companies",
        table_index=0,
        symbol_column="Ticker symbol",
        name_column="Company",
        sector_column="GICS economic sector",
        industry_column="GICS sub-industry",
        region="US",
        exchange="NYSE/Nasdaq",
        fallback_table_indices=(1,),
    ),
    WikipediaIndexEntry(
        source_id="wikipedia.nasdaq100",
        url="https://en.wikipedia.org/wiki/Nasdaq-100",
        table_index=4,
        symbol_column="Ticker",
        name_column="Company",
        sector_column="GICS Sector",
        industry_column="GICS Sub-Industry",
        region="US",
        exchange="Nasdaq",
        fallback_table_indices=(3, 2, 1),
    ),
    WikipediaIndexEntry(
        source_id="wikipedia.dow30",
        url="https://en.wikipedia.org/wiki/Dow_Jones_Industrial_Average",
        table_index=1,
        symbol_column="Symbol",
        name_column="Company",
        sector_column="Industry",
        region="US",
        exchange="NYSE",
        fallback_table_indices=(2, 0),
    ),
    WikipediaIndexEntry(
        source_id="wikipedia.tsx60",
        url="https://en.wikipedia.org/wiki/S%26P/TSX_60",
        table_index=0,
        symbol_column="Symbol",
        name_column="Company",
        sector_column="Sector",
        suffix=".TO",
        region="CA",
        exchange="TSX",
        currency="CAD",
    ),
    # Europe
    WikipediaIndexEntry(
        source_id="wikipedia.ftse100",
        url="https://en.wikipedia.org/wiki/FTSE_100_Index",
        table_index=4,
        symbol_column="Ticker",
        name_column="Company",
        sector_column="FTSE industry classification benchmark sector",
        suffix=".L",
        region="UK",
        exchange="LSE",
        currency="GBP",
        fallback_table_indices=(3, 5, 6),
    ),
    WikipediaIndexEntry(
        source_id="wikipedia.ftse250",
        url="https://en.wikipedia.org/wiki/FTSE_250_Index",
        table_index=2,
        symbol_column="Ticker",
        name_column="Company",
        suffix=".L",
        region="UK",
        exchange="LSE",
        currency="GBP",
        fallback_table_indices=(1, 3, 4),
    ),
    WikipediaIndexEntry(
        source_id="wikipedia.dax40",
        url="https://en.wikipedia.org/wiki/DAX",
        table_index=4,
        symbol_column="Ticker",
        name_column="Company",
        sector_column="Prime sector",
        suffix=".DE",
        region="EU",
        exchange="XETRA",
        currency="EUR",
        fallback_table_indices=(3, 5, 2),
    ),
    WikipediaIndexEntry(
        source_id="wikipedia.mdax",
        url="https://en.wikipedia.org/wiki/MDAX",
        table_index=1,
        symbol_column="Symbol",
        name_column="Company",
        sector_column="Sector",
        suffix=".DE",
        region="EU",
        exchange="XETRA",
        currency="EUR",
        fallback_table_indices=(0, 2),
    ),
    WikipediaIndexEntry(
        source_id="wikipedia.cac40",
        url="https://en.wikipedia.org/wiki/CAC_40",
        table_index=4,
        symbol_column="Ticker",
        name_column="Company",
        sector_column="Sector",
        suffix=".PA",
        region="EU",
        exchange="EPA",
        currency="EUR",
        fallback_table_indices=(3, 5),
    ),
    WikipediaIndexEntry(
        source_id="wikipedia.aex25",
        url="https://en.wikipedia.org/wiki/AEX_index",
        table_index=2,
        symbol_column="Ticker symbol",
        name_column="Company",
        sector_column="ICB Sector",
        suffix=".AS",
        region="EU",
        exchange="AEX",
        currency="EUR",
        fallback_table_indices=(1, 3),
    ),
    WikipediaIndexEntry(
        source_id="wikipedia.ibex35",
        url="https://en.wikipedia.org/wiki/IBEX_35",
        table_index=2,
        symbol_column="Ticker",
        name_column="Company",
        sector_column="ICB sector",
        suffix=".MC",
        region="EU",
        exchange="BME",
        currency="EUR",
        fallback_table_indices=(1, 3),
    ),
    WikipediaIndexEntry(
        source_id="wikipedia.smi20",
        url="https://en.wikipedia.org/wiki/Swiss_Market_Index",
        table_index=2,
        symbol_column="Ticker symbol",
        name_column="Company",
        sector_column="Sector",
        suffix=".SW",
        region="EU",
        exchange="SIX",
        currency="CHF",
        fallback_table_indices=(1, 3),
    ),
    WikipediaIndexEntry(
        source_id="wikipedia.omxs30",
        url="https://en.wikipedia.org/wiki/OMX_Stockholm_30",
        table_index=0,
        symbol_column="Ticker symbol",
        name_column="Company",
        sector_column="Sector",
        suffix=".ST",
        region="EU",
        exchange="STO",
        currency="SEK",
        fallback_table_indices=(1,),
    ),
    # Asia / Oceania
    WikipediaIndexEntry(
        source_id="wikipedia.nikkei225",
        url="https://en.wikipedia.org/wiki/Nikkei_225",
        table_index=1,
        symbol_column="Ticker",
        name_column="Company",
        sector_column="Sector",
        suffix=".T",
        region="JP",
        exchange="TSE",
        currency="JPY",
        fallback_table_indices=(2, 0, 3),
    ),
    WikipediaIndexEntry(
        source_id="wikipedia.topix100",
        url="https://en.wikipedia.org/wiki/TOPIX_100",
        table_index=1,
        symbol_column="Code",
        name_column="Name",
        sector_column="Sector",
        suffix=".T",
        region="JP",
        exchange="TSE",
        currency="JPY",
        fallback_table_indices=(0, 2),
    ),
    WikipediaIndexEntry(
        source_id="wikipedia.hsi",
        url="https://en.wikipedia.org/wiki/Hang_Seng_Index",
        table_index=3,
        symbol_column="Ticker",
        name_column="Company",
        sector_column="Industry",
        suffix=".HK",
        region="HK",
        exchange="HKEX",
        currency="HKD",
        fallback_table_indices=(2, 4, 1),
    ),
    WikipediaIndexEntry(
        source_id="wikipedia.asx200",
        url="https://en.wikipedia.org/wiki/S%26P/ASX_200",
        table_index=2,
        symbol_column="Code",
        name_column="Company",
        sector_column="GICS Sector",
        suffix=".AX",
        region="AU",
        exchange="ASX",
        currency="AUD",
        fallback_table_indices=(1, 3),
    ),
    WikipediaIndexEntry(
        source_id="wikipedia.straitstimes",
        url="https://en.wikipedia.org/wiki/Straits_Times_Index",
        table_index=1,
        symbol_column="Symbol",
        name_column="Company",
        sector_column="ICB sector",
        suffix=".SI",
        region="SG",
        exchange="SGX",
        currency="SGD",
        fallback_table_indices=(0, 2),
    ),
)


def get_wikipedia_sources(
    *,
    enabled_source_ids: Optional[Iterable[str]] = None,
    overrides: Optional[Mapping[str, Mapping[str, object]]] = None,
) -> list[WikipediaSource]:
    """Materialise ``WikipediaSource`` adapters from the static registry."""
    enabled = {s for s in (enabled_source_ids or [])}
    sources: list[WikipediaSource] = []
    for entry in WIKIPEDIA_INDEX_REGISTRY:
        if enabled and entry.source_id not in enabled:
            continue
        sources.append(WikipediaSource(entry))
    return sources
