"""Universe symbol selection for governed model-training backfills."""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

from data.universe import UniverseManager
from data.universe_tiers import load_universe_tiers
from data.universe_builder import _to_yf_symbol


def normalize_training_symbol(raw: str) -> str | None:
    """Best-effort yfinance normalization for symbols persisted in tiers."""
    sym = str(raw or "").strip().upper()
    if not sym:
        return None
    if "/" in sym:
        base, quote = [p.strip().upper() for p in sym.split("/", 1)]
        if base == "XBT":
            base = "BTC"
        if quote in {"USD", "USDT", "USDC"}:
            return f"{base}-USD"
        return None
    for suffix in ("USDT", "USDC"):
        if sym.endswith(suffix) and len(sym) > len(suffix):
            base = sym[: -len(suffix)]
            if base == "XBT":
                base = "BTC"
            return f"{base}-USD"
    if sym == "VIX":
        return "^VIX"
    if sym == "DXY":
        return "DX-Y.NYB"
    return sym


def _dedupe_symbols(rows: Iterable[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in rows:
        sym = normalize_training_symbol(raw)
        if not sym or sym in seen:
            continue
        seen.add(sym)
        out.append(sym)
    return out


def fallback_static_universe_symbols() -> list[str]:
    """Return yfinance-normalized symbols from the curated static universe."""
    rows: list[str] = []
    for inst in UniverseManager.INITIAL_UNIVERSE:
        sym = _to_yf_symbol(inst.broker_symbol or inst.symbol, inst.broker)
        if sym:
            rows.append(sym)
    return _dedupe_symbols(rows)


def load_training_universe_symbols(
    *,
    tiers_path: str | Path | None = None,
    scope: str = "core,scan",
    max_symbols: int | None = None,
    fallback_to_static: bool = True,
) -> list[str]:
    """
    Load symbols from ``data/runtime/universe_tiers.json`` for historical
    training backfills.

    ``scope`` is a comma-separated combination of ``core``, ``scan``,
    ``light`` or ``all``. If runtime tiers are missing and
    ``fallback_to_static`` is true, the curated static universe is used.
    """
    wanted = {part.strip().lower() for part in str(scope or "").split(",") if part.strip()}
    if not wanted:
        wanted = {"core", "scan"}
    if "all" in wanted:
        wanted = {"core", "scan", "light"}

    path = Path(tiers_path) if tiers_path else None
    tiers = load_universe_tiers(path)
    rows: list[str] = []
    if tiers is not None:
        if "core" in wanted:
            rows.extend(tiers.core)
        if "scan" in wanted:
            rows.extend(tiers.scan)
        if "light" in wanted:
            rows.extend(tiers.light)

    symbols = _dedupe_symbols(rows)
    if not symbols and fallback_to_static:
        symbols = fallback_static_universe_symbols()

    if max_symbols is not None and max_symbols > 0:
        symbols = symbols[: int(max_symbols)]
    return symbols
