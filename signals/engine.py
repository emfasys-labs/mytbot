"""
signals/engine.py
==================
The Signal Engine aggregates outputs from all active strategies
and produces a unified Signal ready for the Risk Engine.

Flow:
    Strategy A → raw signal
    Strategy B → raw signal       → Signal Engine → Signal → Risk Engine
    AI modifier → news score
"""

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Optional
import uuid
from datetime import datetime, timezone
import logging

logger = logging.getLogger(__name__)


@dataclass
class RawSignal:
    """Output from an individual strategy."""
    strategy: str
    symbol: str
    side: str                   # "buy" | "sell" | "hold"
    confidence: float           # 0.0 → 1.0
    broker: str                 # preferred execution venue
    asset_class: str
    metadata: dict = field(default_factory=dict)


@dataclass
class Signal:
    """Unified signal ready for the Risk Engine."""
    signal_id: str
    symbol: str
    side: str
    strategy: str
    confidence: float
    suggested_quantity: Decimal
    suggested_price: Optional[Decimal]
    broker: str
    asset_class: str
    timestamp: str
    metadata: dict = field(default_factory=dict)
    news_score: Optional[float] = None      # from AI layer (M6)
    news_veto: bool = False                 # AI vetoed this trade


class SignalEngine:
    """
    Receives raw signals from strategies.
    Applies AI news modifier (M6).
    Outputs a unified Signal for the Risk Engine.
    """

    def __init__(self, config: dict):
        self.config = config

    def process(
        self,
        raw: RawSignal,
        portfolio_value: Decimal,
        news_score: Optional[float] = None,
    ) -> Optional[Signal]:
        """
        Convert a raw strategy signal into a unified Signal.
        Returns None if signal should be discarded (e.g. news veto).
        """

        # AI news modifier — if news score is strongly negative, veto
        news_veto = False
        if news_score is not None:
            veto_threshold = self.config.get("news_veto_threshold", -0.7)
            if news_score < veto_threshold:
                logger.info(f"Signal vetoed by news score {news_score:.2f} | {raw.symbol}")
                news_veto = True

        # Adjust confidence with news score
        adjusted_confidence = raw.confidence
        if news_score is not None:
            # News boosts or reduces confidence
            adjustment = news_score * self.config.get("news_confidence_weight", 0.15)
            adjusted_confidence = max(0.0, min(1.0, raw.confidence + adjustment))

        # Size the position (simple fixed-fraction for now — M4 risk engine refines this)
        position_pct = self.config.get("default_position_pct", 0.05)
        last_price = self._extract_last_price(raw.metadata)
        suggested_quantity = self._calculate_quantity(
            portfolio_value,
            position_pct,
            raw.symbol,
            last_price=last_price,
        )

        signal = Signal(
            signal_id=str(uuid.uuid4()),
            symbol=raw.symbol,
            side=raw.side,
            strategy=raw.strategy,
            confidence=adjusted_confidence,
            suggested_quantity=suggested_quantity,
            suggested_price=last_price,
            broker=raw.broker,
            asset_class=raw.asset_class,
            timestamp=datetime.now(timezone.utc).isoformat(),
            metadata=raw.metadata,
            news_score=news_score,
            news_veto=news_veto,
        )

        logger.info(
            f"Signal {'VETOED' if news_veto else 'GENERATED'} | "
            f"{signal.symbol} {signal.side} | "
            f"confidence={signal.confidence:.2f} | "
            f"strategy={signal.strategy}"
        )

        return signal if not news_veto else None

    def _calculate_quantity(
        self,
        portfolio_value: Decimal,
        position_pct: float,
        symbol: str,
        *,
        last_price: Optional[Decimal],
    ) -> Decimal:
        """
        Calculate position size as a fraction of portfolio.
        TODO M4: replace with Kelly Criterion or volatility-adjusted sizing.
        """
        notional = portfolio_value * Decimal(str(position_pct))
        min_qty = Decimal(str(self.config.get("min_quantity", "0.0001")))
        qty_decimals = int(self.config.get("quantity_decimals", 8))
        tick = Decimal("1").scaleb(-qty_decimals)

        if last_price is None or last_price <= 0:
            # Fallback remains notional-denominated until pricing is known.
            return notional.quantize(tick)

        quantity = (notional / last_price).quantize(tick)
        if quantity < min_qty:
            quantity = min_qty
        return quantity

    @staticmethod
    def _extract_last_price(metadata: dict) -> Optional[Decimal]:
        for key in ("close", "last_price", "price"):
            if key not in metadata:
                continue
            try:
                price = Decimal(str(metadata[key]))
            except (InvalidOperation, TypeError, ValueError):
                continue
            if price > 0:
                return price
        return None
