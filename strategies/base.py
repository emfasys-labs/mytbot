"""
strategies/base.py
==================
Abstract base class for all trading strategies.

Every strategy must implement:
- generate_signal(): given feature data, produce a RawSignal or None

Strategies are completely isolated from brokers and execution.
They only see feature data — prices, indicators, news scores.
They never touch the broker API directly.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional
import pandas as pd

from signals.engine import RawSignal


class Strategy(ABC):
    """
    Abstract base class for all strategies.
    Add a new strategy: create a new file, subclass this, implement generate_signal().
    Register in main.py — no other changes needed.
    """

    name: str = "base"
    preferred_broker: str = "ibkr"
    asset_class: str = "equity"

    def __init__(self, config: dict):
        self.config = config
        self.enabled = config.get("enabled", True)
        # Class-level ``asset_class`` is the *default*. A strategy may declare
        # multiple supported asset classes in YAML via ``asset_classes: [...]``
        # which is the preferred form going forward. We always keep the legacy
        # ``asset_class`` scalar in sync with the first entry so downstream
        # consumers that only read the scalar keep working.
        acs_raw = config.get("asset_classes")
        if isinstance(acs_raw, (list, tuple)) and acs_raw:
            acs = [str(a).strip().lower() for a in acs_raw if str(a).strip()]
        else:
            scalar = config.get("asset_class", self.asset_class)
            acs = [str(scalar).strip().lower()] if scalar else [self.asset_class]
        seen: list[str] = []
        for a in acs:
            if a and a not in seen:
                seen.append(a)
        self.supported_asset_classes: list[str] = seen or [self.asset_class]
        # Default scalar is whichever the operator listed first — lets a
        # strategy declare ``[crypto, equity]`` to prefer crypto labelling.
        self.asset_class = self.supported_asset_classes[0]

    def supports_asset_class(self, asset_class: str) -> bool:
        """Return True when this strategy should evaluate symbols of this class."""
        if not asset_class:
            return False
        return str(asset_class).strip().lower() in self.supported_asset_classes

    @abstractmethod
    def generate_signal(
        self,
        symbol: str,
        features: pd.DataFrame,
    ) -> Optional[RawSignal]:
        """
        Given a feature dataframe (OHLCV + indicators), return a RawSignal or None.
        None means no trade opportunity detected.
        """
        ...

    def __repr__(self) -> str:
        status = "ON" if self.enabled else "OFF"
        return f"<Strategy name={self.name} status={status}>"
