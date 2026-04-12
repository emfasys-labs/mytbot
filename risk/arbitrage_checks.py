from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Optional


@dataclass
class ArbitrageVenueState:
    """Minimal stand-in until treasury exposes real concentration metrics."""

    concentrations: dict[str, Decimal]

    def exchange_concentration(self, venue: str) -> Decimal:
        return self.concentrations.get(venue.strip().lower(), Decimal("0"))


class ArbitrageRiskChecks:
    def __init__(self, config: dict, logger: Any | None = None) -> None:
        self._config = config
        self._logger = logger

    def validate_funding_signal(self, signal: Any, portfolio_state: dict, venue_state: ArbitrageVenueState) -> tuple[bool, str]:
        if not self._config.get("enabled", True):
            return (True, "arbitrage_disabled")

        max_total = Decimal(str(self._config.get("max_total_arbitrage_exposure", "1")))
        max_single = Decimal(str(self._config.get("max_single_arbitrage_exposure", "1")))
        max_exchange_conc = Decimal(str(self._config.get("max_exchange_concentration", "1")))
        max_basis_expansion = Decimal(str(self._config.get("max_basis_expansion_bps", "9999")))

        meta = signal.metadata if isinstance(getattr(signal, "metadata", None), dict) else {}
        try:
            net_yield = Decimal(str(meta.get("annualised_net_yield", "0")))
        except Exception:  # noqa: BLE001
            net_yield = Decimal("0")
        if net_yield <= 0:
            return (False, "annualised_net_yield_non_positive")

        try:
            basis_bps = Decimal(str(meta.get("basis_bps", "0")))
        except Exception:  # noqa: BLE001
            basis_bps = Decimal("0")
        if abs(basis_bps) > max_basis_expansion:
            return (False, "basis_too_wide")

        arb_ratio = Decimal(str(portfolio_state.get("arbitrage_exposure_ratio", "0")))
        if arb_ratio > max_total:
            return (False, "total_arbitrage_exposure_limit")

        ter = meta.get("target_exposure_ratio")
        if ter is not None:
            try:
                if Decimal(str(ter)) > max_single:
                    return (False, "single_arbitrage_exposure_limit")
            except Exception:  # noqa: BLE001
                pass

        spot_v = str(meta.get("spot_venue", getattr(signal, "broker", ""))).strip().lower()
        perp_v = str(meta.get("perp_venue", "")).strip().lower()
        if spot_v and venue_state.exchange_concentration(spot_v) > max_exchange_conc:
            return (False, "spot_exchange_concentration_limit")
        if perp_v and venue_state.exchange_concentration(perp_v) > max_exchange_conc:
            return (False, "perp_exchange_concentration_limit")

        return (True, "approved")
