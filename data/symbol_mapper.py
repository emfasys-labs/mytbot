"""
Canonical crypto symbols (e.g. BTCUSDT) → per-venue instrument strings for REST APIs.

Kraken uses XBT for BTC in pair codes; Bybit/Binance use contiguous BASEQUOTE.
Does not change ``BrokerAdapter``; adapters may call these helpers for consistency.
"""

from __future__ import annotations

def canonical_symbol(symbol: str) -> str:
    """
    Normalise to uppercase contiguous form: ``BTCUSDT``, ``ETHUSDT``.
    Accepts ``BTC/USDT``, ``btc-usdt``, ``BTCUSDT``.
    """
    s = symbol.strip().upper().replace(" ", "").replace("-", "")
    if "/" in s:
        return s.replace("/", "")
    return s


def _split_base_quote(s: str) -> tuple[str, str] | None:
    s = canonical_symbol(s)
    for suf in ("USDT", "USDC", "USD", "EUR", "GBP", "JPY"):
        if s.endswith(suf) and len(s) > len(suf):
            return s[: -len(suf)], suf
    return None


def kraken_pair_altname(symbol: str) -> str:
    """
    Map human or canonical symbol to Kraken REST pair altname, e.g. ``BTCUSDT`` → ``XBTUSDT``.
    Shared with Kraken adapter; keep in sync with exchange pair listings.
    """
    s = symbol.strip().upper().replace(" ", "")
    if "/" in s:
        base, quote = s.split("/", 1)
    else:
        parsed = _split_base_quote(s)
        if parsed is None:
            return s
        base, quote = parsed

    if base == "BTC":
        base = "XBT"
    return f"{base}{quote}"


def binance_symbol(canonical: str) -> str:
    """Binance spot uses contiguous symbols; default same as canonical."""
    return canonical_symbol(canonical)


def bybit_symbol(canonical: str) -> str:
    """Bybit V5: same as canonical without separators (same as adapter)."""
    return canonical_symbol(canonical).replace("/", "")


def to_venue_symbol(venue: str, canonical: str) -> str:
    """
    Map unified canonical symbol to venue-specific string for ``get_order_book`` / tickers.

    ``venue`` is broker name: ``kraken``, ``binance``, ``bybit``, etc.
    """
    v = venue.strip().lower()
    c = canonical_symbol(canonical)
    if v == "kraken":
        return kraken_pair_altname(c)
    if v in ("binance", "binanceus"):
        return binance_symbol(c)
    if v == "bybit":
        return bybit_symbol(c)
    return c
