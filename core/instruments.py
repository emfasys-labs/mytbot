"""
core/instruments.py
===================
First-class instrument specs shared across risk, execution, and storage.
Options are represented structurally (not as opaque broker strings) so the same
model can later cover equity options, index options, and futures options (FOP).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Any, Mapping, Optional


class OptionRight(Enum):
    CALL = "C"
    PUT = "P"


@dataclass(frozen=True)
class OptionContractSpec:
    """
    Single-leg option definition (US equity/index-style via IBKR OPT).

    *expiry* is YYYYMMDD (IBKR lastTradeDateOrContractMonth for plain options).
    """

    underlying_symbol: str
    expiry: str
    strike: Decimal
    right: OptionRight
    multiplier: int = 100
    exchange: str = "SMART"
    currency: str = "USD"
    sec_type: str = "OPT"

    def position_key(self) -> str:
        """Stable unique key for positions, risk theme checks, and order logs."""
        u = self.underlying_symbol.strip().upper()
        e = self.expiry_yyyymmdd()
        r = self.right.value
        s = str(self.strike)
        return f"{u}|{e}|{r}|{s}"

    def expiry_yyyymmdd(self) -> str:
        raw = (self.expiry or "").strip().replace("-", "")
        if len(raw) == 8 and raw.isdigit():
            return raw
        raise ValueError(f"option expiry must be YYYYMMDD, got {self.expiry!r}")

    @staticmethod
    def _normalize_right(v: object) -> OptionRight:
        if isinstance(v, OptionRight):
            return v
        s = str(v or "").strip().upper()
        if s in ("C", "CALL", "CALLS"):
            return OptionRight.CALL
        if s in ("P", "PUT", "PUTS"):
            return OptionRight.PUT
        raise ValueError(f"invalid option right: {v!r}")

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> OptionContractSpec:
        if not isinstance(d, Mapping):
            raise TypeError("option_contract must be a mapping")
        mult_raw = d.get("multiplier", 100)
        mult = int(mult_raw) if mult_raw is not None else 100
        strike_raw = d.get("strike", None)
        if strike_raw is None:
            raise ValueError("option_contract.strike is required")
        return cls(
            underlying_symbol=str(d.get("underlying_symbol", "") or "").strip(),
            expiry=str(d.get("expiry", "") or "").strip(),
            strike=Decimal(str(strike_raw)),
            right=cls._normalize_right(d.get("right")),
            multiplier=max(1, mult),
            exchange=str(d.get("exchange", "SMART") or "SMART").strip() or "SMART",
            currency=str(d.get("currency", "USD") or "USD").strip() or "USD",
            sec_type=str(d.get("sec_type", "OPT") or "OPT").strip().upper() or "OPT",
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "instrument_type": "option",
            "underlying_symbol": self.underlying_symbol.strip().upper(),
            "expiry": self.expiry_yyyymmdd(),
            "strike": str(self.strike),
            "right": self.right.value,
            "multiplier": int(self.multiplier),
            "exchange": self.exchange,
            "currency": self.currency,
            "sec_type": self.sec_type,
        }


def parse_option_contract_from_metadata(metadata: Optional[Mapping[str, Any]]) -> Optional[OptionContractSpec]:
    """Return spec if *metadata* contains a valid ``option_contract`` payload."""
    if not isinstance(metadata, Mapping):
        return None
    raw = metadata.get("option_contract")
    if not isinstance(raw, Mapping):
        return None
    try:
        return OptionContractSpec.from_dict(raw)
    except (TypeError, ValueError):
        return None


def option_premium_notional(
    qty: Decimal,
    premium_per_contract: Decimal,
    multiplier: int,
) -> Decimal:
    """Premium dollars for *qty* contracts (long pay / short receive magnitude)."""
    return abs(qty) * abs(premium_per_contract) * Decimal(int(multiplier))


# ---------------------------------------------------------------------------
# Futures contract specifications (D165)
# ---------------------------------------------------------------------------
# These are exchange-defined INSTRUMENT FACTS, not tunable strategy parameters:
# one CL contract is 1,000 barrels, one ES point is $50, etc. They are physical
# constants of the contract (exactly like the option ``multiplier=100`` above),
# so they live here as instrument definitions rather than in any config knob.
# The IBKR-qualified contract carries its own ``multiplier`` which is treated as
# the authoritative source at order time; this table is the pre-trade source
# used for sizing (notional → whole contracts) before the broker is contacted.


@dataclass(frozen=True)
class FuturesContractSpec:
    """Static definition of a continuous-future root.

    ``root`` is the IBKR/exchange symbol (``CL``, ``ES``, ...). ``multiplier``
    is the contract point value / size; ``exchange`` is the IB routing
    destination; ``currency`` the contract currency.
    """

    root: str
    exchange: str
    multiplier: Decimal
    currency: str = "USD"
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "instrument_type": "future",
            "root": self.root,
            "exchange": self.exchange,
            "multiplier": str(self.multiplier),
            "currency": self.currency,
            "description": self.description,
        }


# Root → spec. Multipliers/exchanges are standard CME/CBOT/NYMEX/COMEX/ICE-US
# contract specs. Mirrors ``instruments/sources/static_futures.py::FUTURES_ROOTS``.
FUTURES_CONTRACT_SPECS: dict[str, FuturesContractSpec] = {
    # Equity index
    "ES": FuturesContractSpec("ES", "CME", Decimal("50"), description="E-mini S&P 500"),
    "NQ": FuturesContractSpec("NQ", "CME", Decimal("20"), description="E-mini Nasdaq 100"),
    "YM": FuturesContractSpec("YM", "CBOT", Decimal("5"), description="E-mini Dow Jones"),
    "RTY": FuturesContractSpec("RTY", "CME", Decimal("50"), description="E-mini Russell 2000"),
    # Energy
    "CL": FuturesContractSpec("CL", "NYMEX", Decimal("1000"), description="WTI Crude Oil"),
    "BZ": FuturesContractSpec("BZ", "NYMEX", Decimal("1000"), description="Brent Crude"),
    "NG": FuturesContractSpec("NG", "NYMEX", Decimal("10000"), description="Henry Hub Natural Gas"),
    # Metals
    "GC": FuturesContractSpec("GC", "COMEX", Decimal("100"), description="Gold"),
    "SI": FuturesContractSpec("SI", "COMEX", Decimal("5000"), description="Silver"),
    "HG": FuturesContractSpec("HG", "COMEX", Decimal("25000"), description="Copper"),
    "PL": FuturesContractSpec("PL", "NYMEX", Decimal("50"), description="Platinum"),
    "PA": FuturesContractSpec("PA", "NYMEX", Decimal("100"), description="Palladium"),
    # Rates
    "ZN": FuturesContractSpec("ZN", "CBOT", Decimal("1000"), description="10y US Treasury Note"),
    "ZB": FuturesContractSpec("ZB", "CBOT", Decimal("1000"), description="30y US Treasury Bond"),
    "ZF": FuturesContractSpec("ZF", "CBOT", Decimal("1000"), description="5y US Treasury Note"),
    "ZT": FuturesContractSpec("ZT", "CBOT", Decimal("2000"), description="2y US Treasury Note"),
    # Agriculture
    "ZC": FuturesContractSpec("ZC", "CBOT", Decimal("50"), description="Corn"),
    "ZS": FuturesContractSpec("ZS", "CBOT", Decimal("50"), description="Soybeans"),
    "ZW": FuturesContractSpec("ZW", "CBOT", Decimal("50"), description="Wheat"),
    "KC": FuturesContractSpec("KC", "NYBOT", Decimal("37500"), description="Coffee"),
    "SB": FuturesContractSpec("SB", "NYBOT", Decimal("112000"), description="Sugar #11"),
    "CC": FuturesContractSpec("CC", "NYBOT", Decimal("10"), description="Cocoa"),
    "CT": FuturesContractSpec("CT", "NYBOT", Decimal("500"), description="Cotton #2"),
}


def futures_root(symbol: str) -> Optional[str]:
    """Extract the contract root from the yfinance continuous-futures form.

    REQUIRES the ``=F`` suffix (``CL=F`` → ``CL``). The bare root form is
    intentionally NOT matched because many roots collide with real equity
    tickers (``CL`` = Colgate-Palmolive, ``ES`` = Eversource, ``GC``/``SI``/
    ``PA``/``PL``/``HG``/``CC``/``CT`` …). Treating those as futures would
    mis-size the equity by the contract multiplier. The canonical pipeline
    symbol for a future is always ``ROOT=F``; that suffix is preserved all the
    way to the IBKR adapter (see ``broker_symbol_for``) so the contract is
    unambiguous. Case-insensitive.
    """
    if not symbol:
        return None
    s = str(symbol).strip().upper()
    if not s.endswith("=F"):
        return None
    root = s[:-2].strip()
    return root if root in FUTURES_CONTRACT_SPECS else None


def futures_spec_for(symbol: str) -> Optional[FuturesContractSpec]:
    """Return the :class:`FuturesContractSpec` for *symbol* or ``None``."""
    root = futures_root(symbol)
    return FUTURES_CONTRACT_SPECS.get(root) if root else None


def futures_multiplier(symbol: str) -> Optional[Decimal]:
    """Return the contract multiplier for a futures *symbol*, else ``None``.

    Used by sizing so a futures order quantity is expressed in whole contracts:
    ``contracts = notional / (price * multiplier)``. Returns ``None`` for
    non-futures symbols so callers fall back to the 1:1 (share) convention.
    """
    spec = futures_spec_for(symbol)
    return spec.multiplier if spec is not None else None


def normalize_futures_mark_price(
    symbol: str,
    raw_price: Decimal,
    *,
    avg_entry_price: Decimal | None = None,
) -> Decimal:
    """Coerce a venue quote onto per-unit futures marks (e.g. USD/bbl for ``CL=F``).

    Some venues return IB-style contract-cost scales (``price * multiplier``) or
    CFD point scales (``price * 100``). When an entry average is known, pick the
    candidate closest to that anchor within a sane band.
    """
    if raw_price <= 0:
        return raw_price
    mult = futures_multiplier(symbol)
    if mult is None:
        return raw_price

    ref = avg_entry_price if avg_entry_price is not None and avg_entry_price > 0 else None
    candidates: list[Decimal] = [raw_price]
    if mult > 0:
        candidates.append(raw_price / mult)
    for factor in (Decimal("100"), Decimal("10")):
        candidates.append(raw_price / factor)
        if mult > 0:
            candidates.append(raw_price / (mult * factor))

    if ref is not None:
        sane = [
            c
            for c in candidates
            if c > 0 and Decimal("0.25") <= (c / ref) <= Decimal("4")
        ]
        if sane:
            return min(sane, key=lambda c: abs(c - ref))
    return raw_price


def pick_mark_quotes(quotes: list[Decimal], *, ref_price: Decimal | None = None) -> Decimal:
    """Choose a robust mark from one or more venue quotes."""
    cleaned = [q for q in quotes if q > 0]
    if not cleaned:
        return Decimal(0)
    if len(cleaned) == 1:
        return cleaned[0]
    if ref_price is not None and ref_price > 0:
        sane = [
            q
            for q in cleaned
            if Decimal("0.2") <= (q / ref_price) <= Decimal("5")
        ]
        if sane:
            cleaned = sane
    cleaned.sort()
    mid = len(cleaned) // 2
    if len(cleaned) % 2 == 1:
        return cleaned[mid]
    return (cleaned[mid - 1] + cleaned[mid]) / Decimal("2")
