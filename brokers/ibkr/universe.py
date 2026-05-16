from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


DEFAULT_IBKR_UNIVERSE_PATH = Path("config/ibkr_universe.yaml")


@dataclass(frozen=True)
class IBKRUniverseEntry:
    symbol: str
    name: str
    asset_class: str
    broker_symbol: str
    sector: str | None = None
    region: str | None = None
    currency: str = "USD"
    exchange: str = "SMART"
    enabled: bool = True


def _clean_symbol(value: object) -> str:
    return str(value or "").strip().upper()


def _fallback_entries() -> list[IBKRUniverseEntry]:
    from data.universe import UniverseManager

    out: list[IBKRUniverseEntry] = []
    for inst in UniverseManager.INITIAL_UNIVERSE:
        if str(inst.broker or "").strip().lower() != "ibkr":
            continue
        out.append(
            IBKRUniverseEntry(
                symbol=_clean_symbol(inst.symbol),
                name=str(inst.name or "").strip(),
                asset_class=str(inst.asset_class or "").strip().lower() or "equity",
                broker_symbol=_clean_symbol(inst.broker_symbol or inst.symbol),
                sector=inst.sector,
                region=inst.region,
            )
        )
    return out


def load_ibkr_universe(path: str | Path | None = None) -> list[IBKRUniverseEntry]:
    """Load the curated IBKR universe, falling back to the legacy seed."""
    p = Path(path) if path is not None else DEFAULT_IBKR_UNIVERSE_PATH
    if not p.exists():
        return _fallback_entries()

    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    rows = raw.get("instruments") if isinstance(raw, dict) else None
    if not isinstance(rows, list):
        return _fallback_entries()

    entries: list[IBKRUniverseEntry] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        symbol = _clean_symbol(row.get("symbol"))
        broker_symbol = _clean_symbol(row.get("broker_symbol") or symbol)
        if not symbol or not broker_symbol:
            continue
        enabled = bool(row.get("enabled", True))
        if not enabled:
            continue
        key = broker_symbol
        if key in seen:
            continue
        seen.add(key)
        entries.append(
            IBKRUniverseEntry(
                symbol=symbol,
                name=str(row.get("name") or symbol).strip(),
                asset_class=str(row.get("asset_class") or "equity").strip().lower(),
                broker_symbol=broker_symbol,
                sector=_optional_str(row.get("sector")),
                region=_optional_str(row.get("region")),
                currency=_clean_symbol(row.get("currency") or "USD"),
                exchange=_clean_symbol(row.get("exchange") or "SMART"),
                enabled=enabled,
            )
        )
    return entries or _fallback_entries()


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def ibkr_supported_symbol_seed(path: str | Path | None = None) -> list[str]:
    return [entry.broker_symbol for entry in load_ibkr_universe(path)]

