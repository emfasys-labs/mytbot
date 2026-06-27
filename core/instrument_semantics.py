"""Economic instrument roles used by portfolio construction."""

from __future__ import annotations

from typing import Any


_CASH_EQUIVALENT_BASES = frozenset(
    {
        "DAI",
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
