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
from typing import Optional, cast
import uuid
from datetime import datetime, timezone
import logging

from core.models_runtime import AssetClass, Side, SignalCandidate

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

        if (raw.side or "").strip().upper().startswith("ARBITRAGE_"):
            return self._process_arbitrage(raw, portfolio_value, news_score)

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

        # Size the position (fixed fraction; optional ATR scaling; M4 risk refines further)
        position_pct = self.config.get("default_position_pct", 0.05)
        last_price = self._extract_last_price(raw.metadata)
        suggested_quantity = self._calculate_quantity(
            portfolio_value,
            position_pct,
            raw.symbol,
            last_price=last_price,
        )
        qty_decimals = int(self.config.get("quantity_decimals", 8))
        tick = Decimal("1").scaleb(-qty_decimals)
        vs = self.config.get("volatility_sizing")
        if isinstance(vs, dict) and vs.get("enabled"):
            md = raw.metadata or {}
            atr_pct = md.get("atr_pct")
            if atr_pct is not None:
                try:
                    ap = float(atr_pct)
                    if ap > 0:
                        target = float(vs.get("target_atr_pct", 0.02))
                        scale = target / ap
                        mn = float(vs.get("min_scale", 0.25))
                        mx = float(vs.get("max_scale", 2.0))
                        scale = max(mn, min(mx, scale))
                        suggested_quantity = (suggested_quantity * Decimal(str(scale))).quantize(tick)
                except (TypeError, ValueError, InvalidOperation):
                    pass

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

    def _process_arbitrage(
        self,
        raw: RawSignal,
        portfolio_value: Decimal,
        news_score: Optional[float] = None,
    ) -> Optional[Signal]:
        """
        Structural arbitrage: skip directional conflict sizing; carry venue metadata for paired execution.
        News veto optional (default: do not veto carry on headline sentiment alone).
        """
        skip_news = bool(self.config.get("arbitrage_skip_news_veto", True))
        news_veto = False
        if not skip_news and news_score is not None:
            veto_threshold = self.config.get("news_veto_threshold", -0.7)
            if news_score < veto_threshold:
                news_veto = True

        adjusted_confidence = raw.confidence
        if news_score is not None and not skip_news:
            adjustment = news_score * self.config.get("news_confidence_weight", 0.15)
            adjusted_confidence = max(0.0, min(1.0, raw.confidence + adjustment))

        md = dict(raw.metadata or {})
        qty_decimals = int(self.config.get("quantity_decimals", 8))
        tick = Decimal("1").scaleb(-qty_decimals)

        last_price = self._extract_last_price(md)
        if last_price is None or last_price <= 0:
            try:
                sm = md.get("spot_mid")
                if sm is not None:
                    last_price = Decimal(str(sm))
            except (InvalidOperation, TypeError, ValueError):
                last_price = None

        suggested_quantity: Decimal
        if md.get("arbitrage_quantity") is not None:
            try:
                suggested_quantity = Decimal(str(md["arbitrage_quantity"])).quantize(tick)
            except (InvalidOperation, TypeError, ValueError):
                suggested_quantity = Decimal("0")
        elif md.get("target_notional") is not None and last_price and last_price > 0:
            try:
                n = Decimal(str(md["target_notional"]))
                suggested_quantity = (n / last_price).quantize(tick)
            except (InvalidOperation, TypeError, ValueError):
                suggested_quantity = Decimal("0")
        else:
            suggested_quantity = self._calculate_quantity(
                portfolio_value,
                float(self.config.get("arbitrage_position_pct", self.config.get("default_position_pct", 0.02))),
                raw.symbol,
                last_price=last_price,
            )

        min_qty = Decimal(str(self.config.get("min_quantity", "0.0001")))
        if suggested_quantity < min_qty:
            suggested_quantity = min_qty

        risk_notional = md.get("risk_notional_override")
        if risk_notional is None and last_price and last_price > 0:
            risk_notional = str(abs(suggested_quantity * last_price))
        if risk_notional is not None:
            md["risk_notional_override"] = str(risk_notional)

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
            metadata=md,
            news_score=news_score,
            news_veto=news_veto,
        )

        logger.info(
            f"Signal {'VETOED' if news_veto else 'GENERATED'} | "
            f"{signal.symbol} {signal.side} | "
            f"confidence={signal.confidence:.2f} | "
            f"strategy={signal.strategy} | arbitrage"
        )

        return signal if not news_veto else None

    def raw_to_signal_candidate(
        self,
        raw: RawSignal,
        news_score: Optional[float] = None,
    ) -> Optional[SignalCandidate]:
        """
        D015 path: same news gating and confidence adjustment as ``process``, without legacy sizing.
        """
        news_veto = False
        if news_score is not None:
            veto_threshold = self.config.get("news_veto_threshold", -0.7)
            if news_score < veto_threshold:
                return None
        adjusted_confidence = raw.confidence
        if news_score is not None:
            adjustment = news_score * self.config.get("news_confidence_weight", 0.15)
            adjusted_confidence = max(0.0, min(1.0, raw.confidence + adjustment))
        ac = (raw.asset_class or "other").strip().lower()
        if ac not in (
            "equity",
            "etf",
            "bond",
            "forex",
            "crypto",
            "future",
            "option",
            "other",
        ):
            ac = "other"
        side: Side = "long" if (raw.side or "").lower() in ("buy", "long") else "short"
        return SignalCandidate(
            symbol=raw.symbol,
            asset_class=cast(AssetClass, ac),
            side=side,
            timestamp=datetime.now(timezone.utc),
            raw_signal_strength=Decimal(str(raw.confidence)),
            adjusted_signal_strength=Decimal(str(adjusted_confidence)),
            confidence=Decimal(str(adjusted_confidence)),
            strategy_name=raw.strategy,
            metadata=dict(raw.metadata or {}),
        )

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


def unified_signal_to_signal_candidate(signal: Signal) -> SignalCandidate:
    """Sizing-free ``SignalCandidate`` from a unified ``Signal`` (D015 batch path)."""
    ac = (signal.asset_class or "other").strip().lower()
    if ac not in (
        "equity",
        "etf",
        "bond",
        "forex",
        "crypto",
        "future",
        "option",
        "other",
    ):
        ac = "other"
    side: Side = "long" if (signal.side or "").lower() in ("buy", "long") else "short"
    try:
        ts = datetime.fromisoformat(signal.timestamp.replace("Z", "+00:00"))
    except Exception:  # noqa: BLE001
        ts = datetime.now(timezone.utc)
    md = dict(signal.metadata or {})
    if signal.news_score is not None:
        md["news_score"] = signal.news_score
    return SignalCandidate(
        symbol=signal.symbol,
        asset_class=cast(AssetClass, ac),
        side=side,
        timestamp=ts,
        raw_signal_strength=Decimal(str(signal.confidence)),
        adjusted_signal_strength=Decimal(str(signal.confidence)),
        confidence=Decimal(str(signal.confidence)),
        strategy_name=signal.strategy,
        metadata=md,
    )
