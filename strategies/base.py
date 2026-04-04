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
