"""Canonical symbol form + broker translators.

The canonical symbol form mirrors yfinance conventions because that is
already the universal data source. Each connected broker has a small
translator pair so the same canonical instrument can be addressed by the
broker-native string at order-build time.

Examples
--------
- US equity / ETF: ``AAPL`` / ``SPY``
- LSE equity: ``HSBA.L``
- XETRA equity: ``SAP.DE``
- Tokyo equity: ``7203.T``
- HKEX equity: ``0700.HK``
- ASX equity: ``BHP.AX``
- TSX equity: ``RY.TO``
- Euronext Paris equity: ``MC.PA``
- Crypto: ``BTC-USD``
- Forex: ``EURUSD=X``
- Continuous future: ``ES=F``
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Literal, Optional


AssetClassHint = Literal[
    "equity",
    "etf",
    "crypto",
    "fx",
    "future",
    "bond",
    "index",
]


_BROKER_NAMES = {"ibkr", "alpaca", "kraken", "binance", "bybit"}

# yfinance exchange suffix → ISO region/exchange hint
_EXCHANGE_SUFFIX: dict[str, tuple[str, str]] = {
    "L": ("UK", "LSE"),
    "DE": ("EU", "XETRA"),
    "F": ("EU", "FRA"),  # frankfurt fallback when used on non-US tickers
    "PA": ("EU", "EPA"),
    "AS": ("EU", "AEX"),
    "MI": ("EU", "BIT"),
    "MC": ("EU", "BME"),
    "BR": ("EU", "EBR"),
    "ST": ("EU", "STO"),
    "HE": ("EU", "HEL"),
    "OL": ("EU", "OSL"),
    "CO": ("EU", "CPH"),
    "SW": ("EU", "SIX"),
    "T": ("JP", "TSE"),
    "HK": ("HK", "HKEX"),
    "AX": ("AU", "ASX"),
    "TO": ("CA", "TSX"),
    "V": ("CA", "TSXV"),
    "NZ": ("NZ", "NZX"),
    "SA": ("BR", "B3"),
    "MX": ("MX", "BMV"),
}

_BTC_ALIASES = {"BTC", "XBT", "XXBT"}
_USD_ALIASES = {"USD", "USDT", "ZUSD"}
_IBKR_PAXOS_CRYPTO_BASES = {
    "BTC",
    "ETH",
    "LTC",
    "BCH",
    "PAXG",
    "SOL",
    "ADA",
    "DOGE",
    "LINK",
    "MATIC",
    "DOT",
}

_FX_MAJOR_CODES = {
    "EUR", "GBP", "USD", "JPY", "CHF", "AUD", "CAD", "NZD", "NOK", "SEK",
    "DKK", "CNH", "CNY", "HKD", "SGD", "ZAR", "MXN", "PLN", "TRY", "HUF",
    "CZK", "ILS", "RUB", "INR", "BRL",
}

_FUTURE_ROOT_PATTERN = re.compile(r"^[A-Z]{1,3}=F$")
_FX_PAIR_PATTERN = re.compile(r"^[A-Z]{6}=X$")
_YF_SYMBOL_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9._\-=]{0,30}$")


@dataclass(frozen=True)
class CanonicalSymbol:
    """A parsed canonical symbol with derived metadata."""

    symbol: str
    asset_class: AssetClassHint
    region: Optional[str]
    exchange: Optional[str]
    base: Optional[str] = None        # crypto base e.g. BTC for BTC-USD
    quote: Optional[str] = None       # crypto/fx quote e.g. USD
    suffix: Optional[str] = None      # yfinance exchange suffix e.g. .L

    @property
    def is_us(self) -> bool:
        return self.region == "US"


def _clean(value: object) -> str:
    return str(value or "").strip().upper()


def detect_asset_class(symbol: str) -> AssetClassHint:
    """Best-effort asset class from canonical-form symbol alone."""
    s = _clean(symbol)
    if not s:
        return "equity"
    if _FX_PAIR_PATTERN.match(s):
        return "fx"
    if _FUTURE_ROOT_PATTERN.match(s):
        return "future"
    if "-" in s:
        base, _, quote = s.partition("-")
        if quote in _USD_ALIASES and base.isalpha() and len(base) <= 6:
            return "crypto"
    return "equity"


def to_canonical(
    raw: object,
    *,
    broker: Optional[str] = None,
    asset_class_hint: Optional[AssetClassHint] = None,
    region_hint: Optional[str] = None,
) -> Optional[CanonicalSymbol]:
    """Normalise a broker- or vendor-specific symbol to canonical form.

    Returns ``None`` if the symbol cannot be safely normalised. Never raises.
    """
    s = _clean(raw)
    if not s:
        return None
    b = (broker or "").strip().lower()

    # Forex normalisation: brokers use EUR.USD or EURUSD or EURUSD=X
    if "." in s and (asset_class_hint == "fx" or b == "ibkr"):
        parts = [p for p in s.split(".") if p]
        if len(parts) == 2 and all(p.isalpha() and len(p) == 3 for p in parts):
            base, quote = parts
            if base in _FX_MAJOR_CODES and quote in _FX_MAJOR_CODES:
                return CanonicalSymbol(
                    symbol=f"{base}{quote}=X",
                    asset_class="fx",
                    region="Global",
                    exchange="IDEALPRO",
                    base=base,
                    quote=quote,
                )

    if _FX_PAIR_PATTERN.match(s):
        base = s[:3]
        quote = s[3:6]
        return CanonicalSymbol(
            symbol=s,
            asset_class="fx",
            region="Global",
            exchange="IDEALPRO" if (base in _FX_MAJOR_CODES and quote in _FX_MAJOR_CODES) else None,
            base=base,
            quote=quote,
        )

    if asset_class_hint == "fx" and len(s) == 6 and s.isalpha():
        base, quote = s[:3], s[3:]
        if base in _FX_MAJOR_CODES and quote in _FX_MAJOR_CODES:
            return CanonicalSymbol(
                symbol=f"{base}{quote}=X",
                asset_class="fx",
                region="Global",
                exchange="IDEALPRO",
                base=base,
                quote=quote,
            )

    # Crypto pair: broker-specific forms like BTCUSD, XBT/USD, BTC/USDT
    if b in {"kraken", "binance", "bybit"} or asset_class_hint == "crypto":
        crypto = _parse_crypto(s)
        if crypto is not None:
            return crypto

    if "-" in s and s.split("-")[-1] in _USD_ALIASES:
        crypto = _parse_crypto(s)
        if crypto is not None:
            return crypto

    if _FUTURE_ROOT_PATTERN.match(s):
        return CanonicalSymbol(
            symbol=s,
            asset_class="future",
            region="US",
            exchange="CME",
        )

    # Equity / ETF — accept letters, digits, and the yfinance exchange suffix.
    if "." in s:
        head, _, tail = s.partition(".")
        suffix = tail.strip()
        if suffix and head and _YF_SYMBOL_PATTERN.match(s):
            region, exchange = _EXCHANGE_SUFFIX.get(suffix, (region_hint, None))
            return CanonicalSymbol(
                symbol=s,
                asset_class=asset_class_hint or "equity",
                region=region,
                exchange=exchange,
                suffix=f".{suffix}",
            )
        return None

    if not _YF_SYMBOL_PATTERN.match(s):
        return None

    return CanonicalSymbol(
        symbol=s,
        asset_class=asset_class_hint or "equity",
        region=region_hint or "US",
        exchange=None,
    )


def _parse_crypto(raw: str) -> Optional[CanonicalSymbol]:
    s = raw
    if "/" in s:
        base, _, quote = s.partition("/")
    elif "-" in s:
        base, _, quote = s.partition("-")
    elif s.endswith("USDT"):
        base, quote = s[:-4], "USDT"
    elif s.endswith("USD"):
        base, quote = s[:-3], "USD"
    else:
        return None
    base = base.strip()
    quote = quote.strip()
    if not base.isalpha() or not quote.isalpha():
        return None
    if base in _BTC_ALIASES:
        base = "BTC"
    if quote not in _USD_ALIASES:
        return None
    canonical = f"{base}-USD"
    return CanonicalSymbol(
        symbol=canonical,
        asset_class="crypto",
        region="Global",
        exchange=None,
        base=base,
        quote="USD",
    )


def canonical_to_broker(canonical: str | CanonicalSymbol, broker: str) -> Optional[str]:
    """Translate canonical → broker-native symbol. Returns ``None`` if not supported."""
    if isinstance(canonical, CanonicalSymbol):
        sym = canonical.symbol
        asset_class: AssetClassHint = canonical.asset_class
    else:
        parsed = to_canonical(canonical)
        if parsed is None:
            return None
        sym = parsed.symbol
        asset_class = parsed.asset_class
    b = (broker or "").strip().lower()
    if b not in _BROKER_NAMES:
        return None

    if asset_class == "fx":
        if not _FX_PAIR_PATTERN.match(sym):
            return None
        base, quote = sym[:3], sym[3:6]
        if b == "ibkr":
            return f"{base}.{quote}"
        return None  # only IBKR routes spot FX in this system

    if asset_class == "crypto":
        if "-" not in sym:
            return None
        base, _, quote = sym.partition("-")
        if quote != "USD":
            return None
        if b == "kraken":
            kbase = "XBT" if base == "BTC" else base
            return f"{kbase}/USD"
        if b == "binance":
            return f"{base}USDT"
        if b == "bybit":
            return f"{base}USDT"
        if b == "ibkr":
            # IBKR PAXOS supports only a small whitelist and expects the bare
            # base symbol for Crypto contracts. Unsupported crypto must not be
            # surfaced as an equity/stock qualification candidate.
            return base if base in _IBKR_PAXOS_CRYPTO_BASES else None
        if b == "alpaca":
            return f"{base}/USD"
        return None

    if asset_class == "future":
        # All adapters keep the =F form for now; futures routing is gated separately.
        return sym

    # Equity / ETF: brokers other than IBKR rarely support international suffixes
    if "." in sym:
        if b == "ibkr":
            head, _, suffix = sym.partition(".")
            return head  # IBKR uses base ticker + exchange/currency in qualification
        return None  # Alpaca / others won't honour international suffix
    return sym


def from_broker_symbol(broker_symbol: str, broker: str) -> Optional[str]:
    """Translate broker-native → canonical. Convenience wrapper over ``to_canonical``."""
    parsed = to_canonical(broker_symbol, broker=broker)
    return parsed.symbol if parsed is not None else None


def iter_known_brokers() -> Iterable[str]:
    return tuple(sorted(_BROKER_NAMES))
