"""iShares ETF holdings source for the D116 instrument registry.

iShares (BlackRock) publishes daily ``holdings.csv`` files per fund. Each
fund-specific URL is stable but exact slugs change between fund families,
so this module keeps a declarative ``IsharesFundEntry`` registry similar to
the Wikipedia source. CSVs are parsed in a thread (pandas) to keep the
event loop free.

This source is the primary breadth supplier for global single-name
constituents: IVV (S&P 500 daily), IWB/IWM/IWV (Russell), QQQ (Nasdaq-100)
plus regional / sector / bond / commodity ETFs.

Note on robustness: BlackRock occasionally changes column names or adds
'Cash' / 'Foreign Currency' / 'Future' synthetic rows. We filter those
explicitly so the contribution stream stays canonical-symbol-only.
"""

from __future__ import annotations

import asyncio
import io
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable, Mapping, Optional

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


_NON_INSTRUMENT_HOLDING_TYPES = frozenset(
    {
        "cash",
        "cash collateral",
        "cash and/or derivatives",
        "currency",
        "foreign currency",
        "fixed deposit",
        "money market",
        "futures",
        "future",
        "fx forward",
        "swap",
        "currency forward",
        "spot",
        "options",
        "rights",
        "warrant",
    }
)


@dataclass(frozen=True)
class IsharesFundEntry:
    """Declarative iShares fund holdings recipe."""

    source_id: str
    fund_ticker: str
    holdings_url: str
    asset_class: AssetClassHint
    region: str
    currency: str = "USD"
    suffix: Optional[str] = None
    skip_rows_until: str = "Ticker"  # header row marker
    symbol_column_aliases: tuple[str, ...] = (
        "Ticker",
        "Issuer Ticker",
        "Ticker Symbol",
    )
    name_column_aliases: tuple[str, ...] = ("Name", "Issuer Name", "Holding")
    sector_column_aliases: tuple[str, ...] = ("Sector", "GICS Sector", "Industry Sector")
    isin_column_aliases: tuple[str, ...] = ("ISIN",)
    figi_column_aliases: tuple[str, ...] = ("FIGI",)
    holding_type_aliases: tuple[str, ...] = ("Asset Class", "Holding Type")
    cadence_sec: int = 86_400
    fallback_skip_until: tuple[str, ...] = field(default_factory=tuple)


def _find_csv_data_start(text: str, markers: Iterable[str]) -> int:
    """Return the byte/string offset of the header row.

    iShares CSVs typically contain ~10 metadata lines (fund name, NAV, etc.)
    followed by a blank line and then the column header. The marker is the
    first column name of that header.
    """
    for marker in markers:
        marker_lc = marker.strip().lower()
        if not marker_lc:
            continue
        for line in text.splitlines():
            if line.strip().lower().startswith(marker_lc):
                idx = text.find(line)
                if idx >= 0:
                    return idx
    return -1


def _find_column(df: pd.DataFrame, aliases: tuple[str, ...]) -> Optional[str]:
    lower_map = {str(c).strip().lower(): c for c in df.columns}
    for alias in aliases:
        col = lower_map.get(alias.strip().lower())
        if col is not None:
            return col
    return None


def _normalise_ticker(raw: object, suffix: Optional[str]) -> Optional[str]:
    s = str(raw or "").strip().upper()
    if not s or s in {"-", "—", "N/A"}:
        return None
    s = re.sub(r"\s+", "", s)
    if not re.match(r"^[A-Z0-9\.\-]{1,15}$", s):
        return None
    if suffix:
        if not s.endswith(suffix):
            return f"{s}{suffix}"
        return s
    # US dual-class -> yfinance hyphen form (BRK.B -> BRK-B)
    if "." in s and len(s.split(".")[-1]) <= 2:
        head, _, tail = s.rpartition(".")
        s = f"{head}-{tail}"
    return s


class IsharesSource:
    """One iShares fund holdings source."""

    def __init__(self, entry: IsharesFundEntry) -> None:
        self._entry = entry
        self.source_id = entry.source_id
        self.source_version = "ishares.v1"
        self.cadence_sec = entry.cadence_sec

    async def fetch(self, ctx: SourceContext) -> SourceFetchResult:
        entry = self._entry
        # iShares CDN rejects generic bot User-Agents with HTTP 500; we MUST send
        # a realistic browser UA + Accept fingerprint to retrieve the CSV
        # holdings file. We are still polite (rate-limited + cached) so this is
        # not abusive; we just identify like a regular browser download.
        # X-Requested-With: XMLHttpRequest tells the iShares ``.ajax`` endpoint
        # to honor the CSV file download instead of redirecting to the HTML
        # product landing page.
        resp = await polite_get(
            entry.holdings_url,
            headers={
                "Accept": "text/csv,application/octet-stream;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Referer": "https://www.ishares.com/",
                "X-Requested-With": "XMLHttpRequest",
            },
            extra_user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        # Defensive: if iShares served HTML instead of CSV (anti-bot fallback),
        # surface a clean error rather than letting the CSV parser fail mid-way.
        body_preview = resp.content[:128].lstrip().lower()
        if body_preview.startswith(b"<!doctype") or body_preview.startswith(b"<html"):
            raise SourceFetchError(
                f"{entry.source_id}: iShares returned HTML instead of CSV "
                "(anti-bot fallback). Skipping; will retry next cadence."
            )
        text_data = resp.content.decode("utf-8-sig", errors="replace")
        header_offset = _find_csv_data_start(
            text_data,
            (entry.skip_rows_until, *entry.fallback_skip_until),
        )
        if header_offset < 0:
            raise SourceFetchError(
                f"{entry.source_id}: header marker not found in CSV"
            )
        csv_body = text_data[header_offset:]
        try:
            df = await asyncio.to_thread(
                pd.read_csv,
                io.StringIO(csv_body),
                dtype=str,
                keep_default_na=False,
                na_values=["-"],
                on_bad_lines="skip",
            )
        except (ValueError, pd.errors.ParserError) as exc:
            raise SourceFetchError(f"{entry.source_id}: CSV parse error: {exc}") from exc

        symbol_col = _find_column(df, entry.symbol_column_aliases)
        if symbol_col is None:
            raise SourceFetchError(
                f"{entry.source_id}: no ticker column found in {list(df.columns)}"
            )
        name_col = _find_column(df, entry.name_column_aliases)
        sector_col = _find_column(df, entry.sector_column_aliases)
        isin_col = _find_column(df, entry.isin_column_aliases)
        figi_col = _find_column(df, entry.figi_column_aliases)
        holding_type_col = _find_column(df, entry.holding_type_aliases)

        contributions: list[SourceContribution] = []
        seen: set[str] = set()
        for _, row in df.iterrows():
            ticker_raw = row.get(symbol_col)
            holding_type = (
                str(row.get(holding_type_col) or "").strip().lower()
                if holding_type_col
                else ""
            )
            if holding_type in _NON_INSTRUMENT_HOLDING_TYPES:
                continue
            ticker = _normalise_ticker(ticker_raw, entry.suffix)
            if not ticker or ticker in seen:
                continue
            name_val = (str(row.get(name_col) or "").strip() or None) if name_col else None
            sector_val = (str(row.get(sector_col) or "").strip() or None) if sector_col else None
            isin_val = (str(row.get(isin_col) or "").strip() or None) if isin_col else None
            figi_val = (str(row.get(figi_col) or "").strip() or None) if figi_col else None
            asset_class: AssetClassHint = entry.asset_class
            contrib = coerce_contribution(
                ticker,
                asset_class_hint=asset_class,
                region_hint=entry.region,
                display_name=name_val,
                currency=entry.currency,
                sector=sector_val,
                isin=isin_val,
                figi=figi_val,
                external_id=ticker,
                metadata={
                    "source_id": entry.source_id,
                    "fund_ticker": entry.fund_ticker,
                    "holdings_url": entry.holdings_url,
                    "holding_type": holding_type or None,
                },
            )
            if contrib is None:
                continue
            seen.add(ticker)
            contributions.append(contrib)

        if not contributions:
            raise SourceFetchError(
                f"{entry.source_id}: parsed {len(df)} rows but produced 0 contributions"
            )

        logger.info(
            "instruments.ishares | {} contributed rows={}",
            entry.source_id,
            len(contributions),
        )
        return SourceFetchResult(
            source_id=entry.source_id,
            source_version=self.source_version,
            contributions=contributions,
            fetched_at=ctx.started_at,
            partial=False,
        )


def _ishares_us(fund_id_number: str, slug: str, fund_ticker: str) -> str:
    """Construct an iShares US fund holdings CSV URL.

    Format: ``/us/products/<fund_id_number>/<slug>/1467271812596.ajax``
    with ``fileName=<TICKER>_holdings`` (the fund's ticker, not the
    numeric fund id) so iShares returns the holdings CSV rather than a
    generic 500.
    """
    return (
        "https://www.ishares.com/us/products/"
        f"{fund_id_number}/{slug}/1467271812596.ajax?"
        "fileType=csv&fileName="
        f"{fund_ticker.upper()}_holdings&dataType=fund"
    )


def _ishares_uk(fund_id_number: str, slug: str, fund_ticker: str) -> str:
    return (
        "https://www.ishares.com/uk/individual/en/products/"
        f"{fund_id_number}/{slug}/1506575576011.ajax?"
        "fileType=csv&fileName="
        f"{fund_ticker.upper()}_holdings&dataType=fund"
    )


ISHARES_FUND_REGISTRY: tuple[IsharesFundEntry, ...] = (
    # US broad / size-style ETFs
    IsharesFundEntry(
        source_id="ishares.IVV",
        fund_ticker="IVV",
        holdings_url=_ishares_us("239726", "ishares-core-sp-500-etf", "IVV"),
        asset_class="equity",
        region="US",
    ),
    IsharesFundEntry(
        source_id="ishares.IJH",
        fund_ticker="IJH",
        holdings_url=_ishares_us("239763", "ishares-core-sp-midcap-etf", "IJH"),
        asset_class="equity",
        region="US",
    ),
    IsharesFundEntry(
        source_id="ishares.IJR",
        fund_ticker="IJR",
        holdings_url=_ishares_us("239774", "ishares-core-sp-smallcap-etf", "IJR"),
        asset_class="equity",
        region="US",
    ),
    IsharesFundEntry(
        source_id="ishares.IWB",
        fund_ticker="IWB",
        holdings_url=_ishares_us("239707", "ishares-russell-1000-etf", "IWB"),
        asset_class="equity",
        region="US",
    ),
    IsharesFundEntry(
        source_id="ishares.IWM",
        fund_ticker="IWM",
        holdings_url=_ishares_us("239710", "ishares-russell-2000-etf", "IWM"),
        asset_class="equity",
        region="US",
    ),
    IsharesFundEntry(
        source_id="ishares.IWV",
        fund_ticker="IWV",
        holdings_url=_ishares_us("239714", "ishares-russell-3000-etf", "IWV"),
        asset_class="equity",
        region="US",
    ),
    IsharesFundEntry(
        source_id="ishares.IWF",
        fund_ticker="IWF",
        holdings_url=_ishares_us("239706", "ishares-russell-1000-growth-etf", "IWF"),
        asset_class="equity",
        region="US",
    ),
    IsharesFundEntry(
        source_id="ishares.IWD",
        fund_ticker="IWD",
        holdings_url=_ishares_us("239708", "ishares-russell-1000-value-etf", "IWD"),
        asset_class="equity",
        region="US",
    ),
    IsharesFundEntry(
        source_id="ishares.IUSG",
        fund_ticker="IUSG",
        holdings_url=_ishares_us("239671", "ishares-core-sp-us-growth-etf", "IUSG"),
        asset_class="equity",
        region="US",
    ),
    IsharesFundEntry(
        source_id="ishares.IUSV",
        fund_ticker="IUSV",
        holdings_url=_ishares_us("239672", "ishares-core-sp-us-value-etf", "IUSV"),
        asset_class="equity",
        region="US",
    ),
    # US sector ETFs (iShares sector flavours; SPDR XL* sit in their own seed if needed)
    IsharesFundEntry(
        source_id="ishares.IYW",
        fund_ticker="IYW",
        holdings_url=_ishares_us("239522", "ishares-us-technology-etf", "IYW"),
        asset_class="equity",
        region="US",
    ),
    IsharesFundEntry(
        source_id="ishares.IYF",
        fund_ticker="IYF",
        holdings_url=_ishares_us("239508", "ishares-us-financials-etf", "IYF"),
        asset_class="equity",
        region="US",
    ),
    IsharesFundEntry(
        source_id="ishares.IYH",
        fund_ticker="IYH",
        holdings_url=_ishares_us("239511", "ishares-us-healthcare-etf", "IYH"),
        asset_class="equity",
        region="US",
    ),
    IsharesFundEntry(
        source_id="ishares.IYE",
        fund_ticker="IYE",
        holdings_url=_ishares_us("239507", "ishares-us-energy-etf", "IYE"),
        asset_class="equity",
        region="US",
    ),
    IsharesFundEntry(
        source_id="ishares.IYJ",
        fund_ticker="IYJ",
        holdings_url=_ishares_us("239512", "ishares-us-industrials-etf", "IYJ"),
        asset_class="equity",
        region="US",
    ),
    IsharesFundEntry(
        source_id="ishares.IYK",
        fund_ticker="IYK",
        holdings_url=_ishares_us("239514", "ishares-us-consumer-staples-etf", "IYK"),
        asset_class="equity",
        region="US",
    ),
    IsharesFundEntry(
        source_id="ishares.IYC",
        fund_ticker="IYC",
        holdings_url=_ishares_us("239513", "ishares-us-consumer-discretionary-etf", "IYC"),
        asset_class="equity",
        region="US",
    ),
    IsharesFundEntry(
        source_id="ishares.IYR",
        fund_ticker="IYR",
        holdings_url=_ishares_us("239520", "ishares-us-real-estate-etf", "IYR"),
        asset_class="equity",
        region="US",
    ),
    IsharesFundEntry(
        source_id="ishares.IYM",
        fund_ticker="IYM",
        holdings_url=_ishares_us("239518", "ishares-us-basic-materials-etf", "IYM"),
        asset_class="equity",
        region="US",
    ),
    IsharesFundEntry(
        source_id="ishares.IYZ",
        fund_ticker="IYZ",
        holdings_url=_ishares_us("239526", "ishares-us-telecommunications-etf", "IYZ"),
        asset_class="equity",
        region="US",
    ),
    IsharesFundEntry(
        source_id="ishares.IDU",
        fund_ticker="IDU",
        holdings_url=_ishares_us("239517", "ishares-us-utilities-etf", "IDU"),
        asset_class="equity",
        region="US",
    ),
    # International developed
    IsharesFundEntry(
        source_id="ishares.EFA",
        fund_ticker="EFA",
        holdings_url=_ishares_us("239623", "ishares-msci-eafe-etf", "EFA"),
        asset_class="equity",
        region="Global",
    ),
    IsharesFundEntry(
        source_id="ishares.IEFA",
        fund_ticker="IEFA",
        holdings_url=_ishares_us("244049", "ishares-core-msci-eafe-etf", "IEFA"),
        asset_class="equity",
        region="Global",
    ),
    IsharesFundEntry(
        source_id="ishares.EWJ",
        fund_ticker="EWJ",
        holdings_url=_ishares_us("239665", "ishares-msci-japan-etf", "EWJ"),
        asset_class="equity",
        region="JP",
    ),
    IsharesFundEntry(
        source_id="ishares.EWG",
        fund_ticker="EWG",
        holdings_url=_ishares_us("239660", "ishares-msci-germany-etf", "EWG"),
        asset_class="equity",
        region="EU",
    ),
    IsharesFundEntry(
        source_id="ishares.EWU",
        fund_ticker="EWU",
        holdings_url=_ishares_us("239690", "ishares-msci-united-kingdom-etf", "EWU"),
        asset_class="equity",
        region="UK",
    ),
    IsharesFundEntry(
        source_id="ishares.EWQ",
        fund_ticker="EWQ",
        holdings_url=_ishares_us("239659", "ishares-msci-france-etf", "EWQ"),
        asset_class="equity",
        region="EU",
    ),
    IsharesFundEntry(
        source_id="ishares.EWA",
        fund_ticker="EWA",
        holdings_url=_ishares_us("239607", "ishares-msci-australia-etf", "EWA"),
        asset_class="equity",
        region="AU",
    ),
    IsharesFundEntry(
        source_id="ishares.EWC",
        fund_ticker="EWC",
        holdings_url=_ishares_us("239611", "ishares-msci-canada-etf", "EWC"),
        asset_class="equity",
        region="CA",
    ),
    IsharesFundEntry(
        source_id="ishares.EWY",
        fund_ticker="EWY",
        holdings_url=_ishares_us("239692", "ishares-msci-south-korea-etf", "EWY"),
        asset_class="equity",
        region="KR",
    ),
    IsharesFundEntry(
        source_id="ishares.EWT",
        fund_ticker="EWT",
        holdings_url=_ishares_us("239689", "ishares-msci-taiwan-etf", "EWT"),
        asset_class="equity",
        region="TW",
    ),
    IsharesFundEntry(
        source_id="ishares.MCHI",
        fund_ticker="MCHI",
        holdings_url=_ishares_us("239619", "ishares-msci-china-etf", "MCHI"),
        asset_class="equity",
        region="CN",
    ),
    IsharesFundEntry(
        source_id="ishares.FXI",
        fund_ticker="FXI",
        holdings_url=_ishares_us("239536", "ishares-china-large-cap-etf", "FXI"),
        asset_class="equity",
        region="CN",
    ),
    IsharesFundEntry(
        source_id="ishares.INDA",
        fund_ticker="INDA",
        holdings_url=_ishares_us("239664", "ishares-msci-india-etf", "INDA"),
        asset_class="equity",
        region="IN",
    ),
    IsharesFundEntry(
        source_id="ishares.EEM",
        fund_ticker="EEM",
        holdings_url=_ishares_us("239637", "ishares-msci-emerging-markets-etf", "EEM"),
        asset_class="equity",
        region="Global",
    ),
    IsharesFundEntry(
        source_id="ishares.IEMG",
        fund_ticker="IEMG",
        holdings_url=_ishares_us("244050", "ishares-core-msci-emerging-markets-etf", "IEMG"),
        asset_class="equity",
        region="Global",
    ),
    IsharesFundEntry(
        source_id="ishares.ACWI",
        fund_ticker="ACWI",
        holdings_url=_ishares_us("239600", "ishares-msci-acwi-etf", "ACWI"),
        asset_class="equity",
        region="Global",
    ),
    IsharesFundEntry(
        source_id="ishares.URTH",
        fund_ticker="URTH",
        holdings_url=_ishares_us("239696", "ishares-msci-world-etf", "URTH"),
        asset_class="equity",
        region="Global",
    ),
    # US fixed income
    IsharesFundEntry(
        source_id="ishares.AGG",
        fund_ticker="AGG",
        holdings_url=_ishares_us("239458", "ishares-core-us-aggregate-bond-etf", "AGG"),
        asset_class="bond",
        region="US",
    ),
    IsharesFundEntry(
        source_id="ishares.TLT",
        fund_ticker="TLT",
        holdings_url=_ishares_us("239454", "ishares-20-year-treasury-bond-etf", "TLT"),
        asset_class="bond",
        region="US",
    ),
    IsharesFundEntry(
        source_id="ishares.IEF",
        fund_ticker="IEF",
        holdings_url=_ishares_us("239456", "ishares-7-10-year-treasury-bond-etf", "IEF"),
        asset_class="bond",
        region="US",
    ),
    IsharesFundEntry(
        source_id="ishares.HYG",
        fund_ticker="HYG",
        holdings_url=_ishares_us("239565", "ishares-iboxx-high-yield-corporate-bond-etf", "HYG"),
        asset_class="bond",
        region="US",
    ),
    IsharesFundEntry(
        source_id="ishares.LQD",
        fund_ticker="LQD",
        holdings_url=_ishares_us("239566", "ishares-iboxx-investment-grade-corporate-bond-etf", "LQD"),
        asset_class="bond",
        region="US",
    ),
    IsharesFundEntry(
        source_id="ishares.SHY",
        fund_ticker="SHY",
        holdings_url=_ishares_us("239452", "ishares-1-3-year-treasury-bond-etf", "SHY"),
        asset_class="bond",
        region="US",
    ),
    IsharesFundEntry(
        source_id="ishares.MUB",
        fund_ticker="MUB",
        holdings_url=_ishares_us("239766", "ishares-national-amt-free-muni-bond-etf", "MUB"),
        asset_class="bond",
        region="US",
    ),
)


def get_ishares_sources(
    *,
    enabled_source_ids: Optional[Iterable[str]] = None,
    overrides: Optional[Mapping[str, Mapping[str, object]]] = None,
) -> list[IsharesSource]:
    """Materialise ``IsharesSource`` adapters from the static registry."""
    enabled = {s for s in (enabled_source_ids or [])}
    sources: list[IsharesSource] = []
    for entry in ISHARES_FUND_REGISTRY:
        if enabled and entry.source_id not in enabled:
            continue
        sources.append(IsharesSource(entry))
    return sources
