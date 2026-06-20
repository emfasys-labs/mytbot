"""Presentation metadata helpers for instruments.

This module is intentionally small and deterministic: it supplies real logo
URLs for well-known companies/funds, crypto icon URLs by ticker, and FX flag
URLs without requiring API keys or storing image binaries.
"""

from __future__ import annotations

from typing import Any


COMPANY_DOMAINS: dict[str, str] = {
    "AAPL": "apple.com",
    "ADEA": "adeia.com",
    "ASML": "asml.com",
    "BARC": "barclays.co.uk",
    "COIN": "coinbase.com",
    "DAL": "delta.com",
    "GOOGL": "abc.xyz",
    "GOOG": "abc.xyz",
    "JPM": "jpmorganchase.com",
    "LMT": "lockheedmartin.com",
    "META": "meta.com",
    "MSFT": "microsoft.com",
    "NVDA": "nvidia.com",
    "TSLA": "tesla.com",
}


FUND_DOMAINS: dict[str, str] = {
    "CORN": "teucrium.com",
    "EEM": "ishares.com",
    "EWG": "ishares.com",
    "EWJ": "ishares.com",
    "FXI": "ishares.com",
    "GDX": "vaneck.com",
    "GLD": "spdrgoldshares.com",
    "HYG": "ishares.com",
    "IWM": "ishares.com",
    "QQQ": "invesco.com",
    "SPY": "ssga.com",
    "TLT": "ishares.com",
    "USO": "uscfinvestments.com",
    "XLE": "ssga.com",
}


FX_FLAG_CODES: dict[str, str] = {
    "AUD": "au",
    "CAD": "ca",
    "CHF": "ch",
    "EUR": "eu",
    "GBP": "gb",
    "JPY": "jp",
    "NZD": "nz",
    "USD": "us",
}


COMMODITY_LOGOS: dict[str, str] = {
    "CL=F": "https://www.google.com/s2/favicons?domain=cmegroup.com&sz=128",
    "GC=F": "https://www.google.com/s2/favicons?domain=cmegroup.com&sz=128",
    "SI=F": "https://www.google.com/s2/favicons?domain=cmegroup.com&sz=128",
    "ES=F": "https://www.google.com/s2/favicons?domain=cmegroup.com&sz=128",
    "NQ=F": "https://www.google.com/s2/favicons?domain=cmegroup.com&sz=128",
}

CRYPTO_DISPLAY_NAMES: dict[str, str] = {
    "ALGO": "Algorand",
    "AR": "Arweave",
    "ATOM": "Cosmos",
    "BTC": "Bitcoin",
    "CAKE": "PancakeSwap",
    "CRV": "Curve DAO",
    "CVX": "Convex Finance",
    "DCR": "Decred",
    "DEXE": "DeXe",
    "DOGE": "Dogecoin",
    "DOT": "Polkadot",
    "EGLD": "MultiversX",
    "ETH": "Ethereum",
    "FET": "Fetch.ai",
    "FIL": "Filecoin",
    "ICP": "Internet Computer",
    "INJ": "Injective",
    "KSM": "Kusama",
    "LINK": "Chainlink",
}


def favicon_url(domain: str) -> str:
    return f"https://www.google.com/s2/favicons?domain={domain}&sz=128"


def crypto_symbol(symbol: str) -> str | None:
    s = str(symbol or "").strip().upper()
    if not s:
        return None
    if s.endswith("-USD"):
        return s[:-4].lower()
    if s.endswith("USD") and len(s) > 3:
        return s[:-3].lower()
    return None


def crypto_display_name(symbol: str) -> str | None:
    code = crypto_symbol(symbol)
    if not code:
        return None
    upper = code.upper()
    return CRYPTO_DISPLAY_NAMES.get(upper) or upper


def fx_flag_url(symbol: str) -> str | None:
    s = str(symbol or "").strip().upper().replace("=X", "")
    if len(s) < 6:
        return None
    code = FX_FLAG_CODES.get(s[:3])
    if not code:
        return None
    return f"https://flagcdn.com/w80/{code}.png"


def logo_url_for_symbol(
    symbol: str,
    *,
    asset_class: str | None = None,
    klass: str | None = None,
) -> str | None:
    s = str(symbol or "").strip().upper()
    ac = str(asset_class or klass or "").strip().lower()
    if not s:
        return None
    if s in COMPANY_DOMAINS:
        return favicon_url(COMPANY_DOMAINS[s])
    if s in FUND_DOMAINS:
        return favicon_url(FUND_DOMAINS[s])
    if s in COMMODITY_LOGOS:
        return COMMODITY_LOGOS[s]
    if ac in {"forex", "fx"} or s.endswith("=X") or (len(s) == 6 and s.isalpha()):
        return fx_flag_url(s)
    crypto = crypto_symbol(s)
    if ac == "crypto" or crypto:
        return f"https://assets.coincap.io/assets/icons/{crypto or s.lower()}@2x.png"
    return None


def with_logo(profile: dict[str, Any], symbol: str, *, asset_class: str | None = None, klass: str | None = None) -> dict[str, Any]:
    if profile.get("logo_url"):
        return profile
    logo = logo_url_for_symbol(symbol, asset_class=asset_class, klass=klass)
    if logo:
        return {**profile, "logo_url": logo}
    return profile
