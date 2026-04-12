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
