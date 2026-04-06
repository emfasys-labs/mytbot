"""
execution/router.py
====================
Smart Order Router (SOR).

Given a signal, decides which broker gives the best execution:
- Is the asset available on this broker?
- What's the current spread?
- What are the fees?
- Is the broker currently healthy?

The router returns the optimal broker name.
The execution engine then routes the order there.

Initially simple — just checks availability.
Later: adds real-time spread comparison across brokers.
"""

import logging
from decimal import Decimal
from typing import Optional

from brokers.permissions import get_permissions

logger = logging.getLogger(__name__)

# Which assets each broker can trade
# This gets extended as brokers are added
BROKER_ASSET_MAP = {
    "ibkr": {
        "equity", "etf", "bond", "forex", "option", "future", "crypto"
    },
    "kraken": {
        "crypto"
    },
    "binance": {
        "crypto"
    },
    "alpaca": {
        "equity", "etf", "crypto"
    },
    # Adding new broker: just add its entry here
    # "bybit": {"crypto", "future"},
    # "deribit": {"crypto", "option"},
}

# Fee tiers per broker (taker fee, used for routing preference)
BROKER_FEE_MAP = {
    "ibkr":    Decimal("0.0018"),   # ~0.18% crypto, ~$0.005/share equities
    "kraken":  Decimal("0.0040"),   # 0.40% taker base
    "binance": Decimal("0.0010"),   # 0.10% base
    "alpaca":  Decimal("0.0000"),   # zero commission equities
}


class SmartOrderRouter:

    def __init__(self, available_brokers: list[str]):
        self.available_brokers = available_brokers
        self.permissions = get_permissions()

    def route(self, asset_class: str, symbol: str) -> Optional[str]:
        """
        Return the best broker name for this asset class and symbol.
        Priority: availability → lowest fee → IBKR as tiebreaker.
        """

        # Filter to brokers that support this asset class
        eligible = [
            b for b in self.available_brokers
            if asset_class in BROKER_ASSET_MAP.get(b, set())
        ]

        if not eligible:
            logger.warning(
                "No broker available by asset map | asset_class=%s symbol=%s",
                asset_class,
                symbol,
            )
            return None

        permitted = [
            b for b in eligible if self.permissions.check_permission(b, asset_class)
        ]
        if not permitted:
            logger.warning(
                "No broker permitted for asset_class=%s symbol=%s | eligible=%s",
                asset_class,
                symbol,
                eligible,
            )
            return None

        # Sort by fee (ascending) — cheapest first
        permitted.sort(key=lambda b: BROKER_FEE_MAP.get(b, Decimal("0.01")))

        # IBKR is preferred for non-crypto (regulatory safety, multi-asset)
        if asset_class != "crypto" and "ibkr" in permitted:
            return "ibkr"

        chosen = permitted[0]
        logger.debug("Routing %s (%s) -> %s", symbol, asset_class, chosen)
        return chosen

    def check_permission(self, broker: str, asset_class: str) -> bool:
        return self.permissions.check_permission(broker, asset_class)

    def get_fallback_broker(self, asset_class: str, exclude: list[str] | None = None) -> Optional[str]:
        candidates = [
            b
            for b in self.available_brokers
            if asset_class in BROKER_ASSET_MAP.get(b, set())
        ]
        return self.permissions.get_fallback_broker(
            asset_class,
            candidates=candidates,
            exclude=exclude or [],
        )

    def reload_permissions(self) -> None:
        self.permissions.reload(force=True)

    def add_broker(self, name: str) -> None:
        """Register a newly connected broker as available for routing."""
        if name not in self.available_brokers:
            self.available_brokers.append(name)
            logger.info(f"Router: added broker {name}")
