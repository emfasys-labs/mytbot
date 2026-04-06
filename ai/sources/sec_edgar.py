from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass
class SecFilingEvent:
    symbol: str
    filing_type: str
    accession_number: str
    filed_at: datetime
    title: str
    url: str
    payload: dict[str, Any]


class SecEdgarSource:
    """M6 scaffold: SEC EDGAR source interface (disabled by default)."""

    def __init__(self, config: dict[str, Any] | None = None):
        self.config = config or {}
        self.enabled = bool(self.config.get("enabled", False))

    async def fetch_recent(self, symbols: list[str], lookback_hours: int = 24) -> list[SecFilingEvent]:
        # Intentionally no live ingestion in this milestone.
        _ = symbols, lookback_hours
        return []
