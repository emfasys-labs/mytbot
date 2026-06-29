"""Economic instrument roles used by portfolio construction.

The trading system must reason about what an instrument *does*, not only how
its broker labels it.  A USD peg is liquidity, wrapped assets are the same
economic exposure as their underlying, and cash-management ETFs are reserves
rather than directional alpha.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any


class InstrumentRole(StrEnum):
    ALPHA = "alpha"
    HEDGE = "hedge"
    CASH_EQUIVALENT = "cash_equivalent"
    LIQUIDITY_RESERVE = "liquidity_reserve"


_CASH_EQUIVALENT_BASES = frozenset(
    {
        "DAI",
        "FIDD",
        "FDUSD",
        "FRAX",
        "GUSD",
        "LUSD",
        "PYUSD",
        "RLUSD",
        "SUSD",
        "TUSD",
        "USD0",
        "USD1",
        "USDC",
        "USDD",
        "USDE",
        "USDG",
        "USDP",
        "USAT",
        "USDT",
    }
)
_LIQUIDITY_RESERVE_SYMBOLS = frozenset({"BOXX", "BIL", "MINT", "SGOV", "SHV"})
_WRAPPED_UNDERLYINGS = {
    "CBETH": "ETH",
    "STETH": "ETH",
    "WBTC": "BTC",
    "WBETH": "ETH",
    "WETH": "ETH",
    "WSTETH": "ETH",
}


def _base_quote(symbol: Any) -> tuple[str, str]:
    value = str(symbol or "").strip().upper().replace("/", "-")
    if "-" in value:
        base, quote = value.split("-", 1)
        return base, quote
    for quote in ("USDT", "USDC", "USD"):
        if value.endswith(quote) and len(value) > len(quote):
            return value[: -len(quote)], quote
    return value, ""


def is_cash_equivalent_pair(symbol: Any) -> bool:
    """True when a USD-anchored treasury asset is being treated as alpha."""
    base, quote = _base_quote(symbol)
    return base in _CASH_EQUIVALENT_BASES and quote in {"USD", "USDC", "USDT"}


def instrument_role(
    symbol: Any,
    *,
    asset_class: Any = "",
    metadata: dict[str, Any] | None = None,
) -> InstrumentRole:
    """Classify the economic role used by allocation and runtime invariants.

    Explicit metadata wins so an operator can mark a bespoke hedge or reserve
    without changing code.  The curated fallbacks cover instruments that are
    structurally not directional alpha.
    """
    md = metadata if isinstance(metadata, dict) else {}
    explicit = str(md.get("instrument_role", "") or "").strip().lower()
    if explicit:
        try:
            return InstrumentRole(explicit)
        except ValueError:
            pass
    raw = str(symbol or "").strip().upper().replace("/", "-")
    if is_cash_equivalent_pair(raw):
        return InstrumentRole.CASH_EQUIVALENT
    canonical = canonical_economic_symbol(raw)
    if canonical.replace("=X", "") in _LIQUIDITY_RESERVE_SYMBOLS:
        return InstrumentRole.LIQUIDITY_RESERVE
    if bool(md.get("hedge")) or str(md.get("portfolio_purpose", "")).lower() == "hedge":
        return InstrumentRole.HEDGE
    return InstrumentRole.ALPHA


def is_directional_alpha(
    symbol: Any,
    *,
    asset_class: Any = "",
    metadata: dict[str, Any] | None = None,
) -> bool:
    return instrument_role(
        symbol,
        asset_class=asset_class,
        metadata=metadata,
    ) in {InstrumentRole.ALPHA, InstrumentRole.HEDGE}


def canonical_economic_symbol(symbol: Any) -> str:
    """Map wrapped representations onto their underlying portfolio exposure."""
    raw = str(symbol or "").strip().upper().replace("/", "-")
    base, quote = _base_quote(raw)
    underlying = _WRAPPED_UNDERLYINGS.get(base)
    if underlying is None:
        return raw
    if "-" in raw:
        return f"{underlying}-{quote}"
    return f"{underlying}{quote}" if quote else underlying
