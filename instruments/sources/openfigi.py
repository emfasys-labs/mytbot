"""OpenFIGI source — bulk ISIN/FIGI + alternate ticker enrichment.

This source is a *secondary enricher*: it does not propose new canonical
symbols on its own. Instead, given the set of already-known canonical
symbols (typically the active registry plus a small static seed), it asks
OpenFIGI to map them to ``isin`` / ``figi`` / alternate exchange tickers,
then writes the enrichment back into the registry as a normal source
contribution.

The free OpenFIGI API tier permits 25 jobs/minute (each job batches up to
100 mapping requests); with an API key (``OPENFIGI_API_KEY``) that rises
to 250 jobs/min. We default to a polite rate well inside the free tier so
operators without an API key can still benefit.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Mapping, Optional, Sequence

from loguru import logger

from instruments.canonical import to_canonical
from instruments.registry import RegistryRow, SourceContribution, coerce_contribution
from instruments.sources.base import (
    SourceContext,
    SourceFetchError,
    SourceFetchResult,
)
from instruments.sources.http import polite_get


OPENFIGI_MAPPING_URL = "https://api.openfigi.com/v3/mapping"
OPENFIGI_BATCH_SIZE = 100
OPENFIGI_DEFAULT_JOBS_PER_MIN = 24  # stay under free-tier 25/min
OPENFIGI_AUTH_JOBS_PER_MIN = 240    # stay under authenticated 250/min


@dataclass(frozen=True)
class OpenFIGIQuery:
    canonical_symbol: str
    id_type: str            # TICKER | ISIN | ID_BB_GLOBAL
    id_value: str
    exch_code: Optional[str] = None
    market_sec_des: Optional[str] = None
    currency: Optional[str] = None


@dataclass
class OpenFIGISource:
    """Secondary enricher.

    Takes an iterable of ``RegistryRow`` (the seed) and emits one
    ``SourceContribution`` per row with refreshed metadata.
    """

    seed: Sequence[RegistryRow] = field(default_factory=tuple)
    api_key_env: str = "OPENFIGI_API_KEY"
    source_id: str = "openfigi"
    source_version: str = "openfigi.v3"
    cadence_sec: int = 604_800  # weekly
    max_symbols: int = 5000

    async def fetch(self, ctx: SourceContext) -> SourceFetchResult:
        api_key = os.getenv(self.api_key_env, "").strip()
        rate_per_min = OPENFIGI_AUTH_JOBS_PER_MIN if api_key else OPENFIGI_DEFAULT_JOBS_PER_MIN
        seconds_per_job = 60.0 / max(1, rate_per_min)

        queries = self._build_queries(self.seed)
        if not queries:
            return SourceFetchResult(
                source_id=self.source_id,
                source_version=self.source_version,
                contributions=(),
                fetched_at=ctx.started_at,
                notes="no seed symbols",
                partial=False,
            )

        contributions: list[SourceContribution] = []
        partial = False
        batches = [queries[i : i + OPENFIGI_BATCH_SIZE] for i in range(0, len(queries), OPENFIGI_BATCH_SIZE)]
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["X-OPENFIGI-APIKEY"] = api_key

        for batch in batches:
            payload = [
                {
                    "idType": q.id_type,
                    "idValue": q.id_value,
                    **({"exchCode": q.exch_code} if q.exch_code else {}),
                    **({"marketSecDes": q.market_sec_des} if q.market_sec_des else {}),
                    **({"currency": q.currency} if q.currency else {}),
                }
                for q in batch
            ]
            try:
                resp = await polite_get(
                    OPENFIGI_MAPPING_URL,
                    method="POST",
                    headers=headers,
                    json_body=payload,
                    cache=False,
                    retries=2,
                )
            except SourceFetchError as exc:
                logger.warning("instruments.openfigi | batch failed (non-fatal): {}", exc)
                partial = True
                await asyncio.sleep(seconds_per_job)
                continue

            try:
                results = json.loads(resp.content.decode("utf-8"))
            except json.JSONDecodeError as exc:
                logger.warning("instruments.openfigi | bad json (non-fatal): {}", exc)
                partial = True
                await asyncio.sleep(seconds_per_job)
                continue

            if not isinstance(results, list) or len(results) != len(batch):
                logger.warning(
                    "instruments.openfigi | result length mismatch (got={}, expected={})",
                    len(results) if isinstance(results, list) else "non-list",
                    len(batch),
                )
                partial = True
                await asyncio.sleep(seconds_per_job)
                continue

            for query, row in zip(batch, results):
                data = (row or {}).get("data") if isinstance(row, dict) else None
                if not data:
                    continue
                primary = data[0]
                figi = primary.get("compositeFIGI") or primary.get("figi")
                contrib = coerce_contribution(
                    query.canonical_symbol,
                    asset_class_hint=self._classify(primary),
                    region_hint=self._region(primary),
                    display_name=primary.get("name") or primary.get("securityDescription"),
                    figi=figi,
                    external_id=primary.get("ticker"),
                    metadata={
                        "openfigi_alternates": [
                            {
                                "ticker": p.get("ticker"),
                                "exch_code": p.get("exchCode"),
                                "security_type": p.get("securityType"),
                                "market_sector": p.get("marketSector"),
                                "currency": p.get("currency"),
                            }
                            for p in data[: min(8, len(data))]
                        ],
                    },
                )
                if contrib is not None:
                    contributions.append(contrib)
            await asyncio.sleep(seconds_per_job)

        logger.info(
            "instruments.openfigi | contributed rows={} partial={}", len(contributions), partial
        )
        return SourceFetchResult(
            source_id=self.source_id,
            source_version=self.source_version,
            contributions=contributions,
            fetched_at=ctx.started_at,
            notes=("authenticated" if api_key else "anonymous"),
            partial=partial,
        )

    def _build_queries(self, seed: Iterable[RegistryRow]) -> list[OpenFIGIQuery]:
        out: list[OpenFIGIQuery] = []
        for row in seed:
            if len(out) >= max(1, int(self.max_symbols)):
                break
            sym = row.canonical_symbol
            base = sym.split("=", 1)[0].split("-", 1)[0]
            base = base.split(".", 1)[0]
            if not base or not re.match(r"^[A-Z0-9]{1,12}$", base):
                continue
            id_type = "ISIN" if row.isin else "TICKER"
            id_value = row.isin or base
            if not id_value:
                continue
            exch_code = _exch_code_for(row)
            out.append(
                OpenFIGIQuery(
                    canonical_symbol=sym,
                    id_type=id_type,
                    id_value=id_value,
                    exch_code=exch_code,
                    market_sec_des=_market_sec_des(row),
                    currency=row.currency or None,
                )
            )
        return out

    @staticmethod
    def _classify(primary: Mapping[str, Any]) -> Any:
        sector = (primary.get("marketSector") or "").strip().lower()
        if "curncy" in sector:
            return "fx"
        if "comdty" in sector:
            return "future"
        sec_type = (primary.get("securityType") or "").strip().lower()
        if "etf" in sec_type or "etp" in sec_type:
            return "etf"
        if "future" in sec_type:
            return "future"
        if "bond" in sec_type:
            return "bond"
        return "equity"

    @staticmethod
    def _region(primary: Mapping[str, Any]) -> Optional[str]:
        exch = (primary.get("exchCode") or "").strip().upper()
        if not exch:
            return None
        if exch in {"US", "UN", "UQ", "UR", "UA", "UF", "UB", "UM", "UD", "UP", "UV"}:
            return "US"
        if exch in {"LN", "LSE"}:
            return "UK"
        if exch in {"GR", "GY", "GF", "GM"}:
            return "EU"
        if exch in {"FP", "FR"}:
            return "EU"
        if exch in {"JT", "JP"}:
            return "JP"
        if exch in {"HK"}:
            return "HK"
        if exch in {"AU"}:
            return "AU"
        if exch in {"CN", "CT"}:
            return "CA"
        return None


def _exch_code_for(row: RegistryRow) -> Optional[str]:
    if not row.exchange:
        return None
    ex = row.exchange.upper()
    table = {
        "LSE": "LN",
        "XETRA": "GY",
        "FRA": "GF",
        "EPA": "FP",
        "TSE": "JT",
        "HKEX": "HK",
        "ASX": "AU",
        "TSX": "CN",
        "BIT": "IM",
        "BME": "SM",
        "SIX": "SW",
        "STO": "SS",
        "OSL": "NO",
        "CPH": "DC",
        "HEL": "FH",
    }
    return table.get(ex)


def _market_sec_des(row: RegistryRow) -> Optional[str]:
    if row.asset_class == "equity":
        return "Equity"
    if row.asset_class == "etf":
        return "Equity"
    if row.asset_class == "bond":
        return "Govt"
    if row.asset_class == "future":
        return "Comdty"
    if row.asset_class == "fx":
        return "Curncy"
    return None
