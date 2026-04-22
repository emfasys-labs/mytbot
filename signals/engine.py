"""
signals/engine.py
==================
The Signal Engine aggregates outputs from all active strategies
and produces a unified Signal ready for the Risk Engine.

Flow:
    Strategy A → raw signal
    Strategy B → raw signal
    Optional SignalAccumulator (time-decayed quant + news + macro)
       → Signal Engine → Signal → Risk Engine
    AI modifier → news score (legacy path) or accumulated net overlay
"""

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Optional, Union, cast
import uuid
from datetime import datetime, timezone
import logging

from core.models_runtime import AssetClass, Side, SignalCandidate

from signals.accumulator import NetSignal, raw_signal_to_input_signal

if TYPE_CHECKING:
    from signals.accumulator import SignalAccumulator

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
    Applies AI news modifier (M6) and optional SignalAccumulator overlay.
    Outputs a unified Signal for the Risk Engine.
    """

    def __init__(self, config: dict, accumulator: Optional["SignalAccumulator"] = None):
        self.config = config
        self.accumulator = accumulator

    def _apply_accumulator(
        self,
        raw: RawSignal,
        *,
        news_score: Optional[float],
        now: datetime,
    ) -> tuple[Optional[NetSignal], Union[Decimal, float, None]]:
        """
        Push quant raw signal into accumulator and return (net_signal, overlay_for_legacy_fields).

        ``overlay_for_legacy_fields`` is ``Decimal`` from the accumulator net when present,
        else point-in-time ``news_score`` (float) for veto/confidence; ``None`` if neither applies.
        """
        if self.accumulator is None:
            return None, None
        inp = raw_signal_to_input_signal(raw, timestamp=now)
        if inp is None:
            net = self.accumulator.compute_net_for_symbol(raw.symbol, now)
            if net is None:
                return None, news_score
            return net, net.score
        net = self.accumulator.update(inp, now)
        return net, net.score

    @staticmethod
    def _enrich_metadata_with_net(md: dict, net: Optional[NetSignal], ai_news_score: Optional[float]) -> None:
        if ai_news_score is not None:
            md["ai_news_score"] = ai_news_score
        if net is None:
            return
        md["accumulator_score"] = str(net.score)
        md["accumulator_confidence"] = str(net.confidence)
        md["accumulator_direction"] = net.direction
        md["accumulator_horizon_bias"] = net.horizon_bias
        md["accumulator_aligned_sources"] = list(net.aligned_sources)
        md["accumulator_conflicting_sources"] = list(net.conflicting_sources)

    def _veto_and_confidence(
        self,
        raw: RawSignal,
        *,
        news_score: Optional[float],
        net: Optional[NetSignal],
        apply_news_overlay: bool,
    ) -> tuple[bool, float]:
        """Returns (news_veto, adjusted_confidence)."""
        if not apply_news_overlay:
            return False, float(raw.confidence)

        veto_threshold = Decimal(str(self.config.get("news_veto_threshold", -0.7)))
        w = Decimal(str(self.config.get("news_confidence_weight", 0.15)))
        dual_ai = bool(self.config.get("accumulator_dual_ai_veto", True))

        overlay_dec: Decimal | None = None
        if self.accumulator is not None and net is not None:
            overlay_dec = net.score
        elif news_score is not None:
            overlay_dec = Decimal(str(news_score))

        news_veto = False
        if overlay_dec is not None and overlay_dec < veto_threshold:
            logger.info(
                "Signal vetoed by overlay score {} (threshold {}) | {}",
                overlay_dec,
                veto_threshold,
                raw.symbol,
            )
            news_veto = True

        # When accumulator produced a net signal, overlay already encodes rolled-up AI/news;
        # do not stack a second veto from stale point-in-time news_score (P1 dual veto).
        if (
            dual_ai
            and self.accumulator is not None
            and net is None
            and news_score is not None
            and Decimal(str(news_score)) < veto_threshold
        ):
            logger.info(
                "Signal vetoed by point AI news score {} (threshold {}) | {}",
                news_score,
                veto_threshold,
                raw.symbol,
            )
            news_veto = True

        base_conf = Decimal(str(raw.confidence))
        if overlay_dec is not None:
            adj = base_conf + overlay_dec * w
            lo, hi = Decimal("0"), Decimal("1")
            if adj < lo:
                adj = lo
            elif adj > hi:
                adj = hi
            adjusted_confidence = float(adj)
        else:
            adjusted_confidence = float(base_conf)

        return news_veto, adjusted_confidence

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

        now = datetime.now(timezone.utc)
        net, _ = self._apply_accumulator(raw, news_score=news_score, now=now)

        news_veto, adjusted_confidence = self._veto_and_confidence(
            raw,
            news_score=news_score,
            net=net,
            apply_news_overlay=True,
        )

        # Size the position.
        #
        # D031 closure — respect coordinator-supplied sizing when present:
        #   1. risk_notional_override   (hard target from risk layer)
        #   2. target_notional          (coordinator/strategy intent)
        #   3. nav * default_position_pct   (legacy fallback)
        #
        # When (1) or (2) is present, the coordinator has already decided the
        # final deployed capital (including volatility and mode adjustments),
        # so we MUST NOT re-apply volatility sizing on top — doing so
        # double-scales and was the cause of the "Sizing boundary guard
        # rejected signal" wave (2x inflation for low-ATR symbols).
        last_price = self._extract_last_price(raw.metadata)
        qty_decimals = int(self.config.get("quantity_decimals", 8))
        tick = Decimal("1").scaleb(-qty_decimals)

        raw_md = raw.metadata or {}

        def _positive_decimal(v: object) -> Optional[Decimal]:
            if v is None:
                return None
            try:
                d = Decimal(str(v))
            except (InvalidOperation, TypeError, ValueError):
                return None
            return d if d > 0 else None

        coord_risk_override = _positive_decimal(raw_md.get("risk_notional_override"))
        coord_target = _positive_decimal(raw_md.get("target_notional"))
        coord_notional = coord_risk_override or coord_target

        if coord_notional is not None and last_price is not None and last_price > 0:
            # Coordinator sizing path — single source of truth.
            suggested_quantity = (coord_notional / last_price).quantize(tick)
            sizing_path = (
                "risk_notional_override" if coord_risk_override is not None else "target_notional"
            )
        else:
            # Legacy fallback: nav * fixed fraction with optional volatility scaling.
            position_pct = self.config.get("default_position_pct", 0.05)
            suggested_quantity = self._calculate_quantity(
                portfolio_value,
                position_pct,
                raw.symbol,
                last_price=last_price,
            )
            sizing_path = "nav_fallback"
            vs = self.config.get("volatility_sizing")
            if isinstance(vs, dict) and vs.get("enabled"):
                atr_pct = raw_md.get("atr_pct")
                if atr_pct is not None:
                    try:
                        ap = float(atr_pct)
                        if ap > 0:
                            target = float(vs.get("target_atr_pct", 0.02))
                            scale = target / ap
                            mn = float(vs.get("min_scale", 0.25))
                            mx = float(vs.get("max_scale", 2.0))
                            scale = max(mn, min(mx, scale))
                            suggested_quantity = (
                                suggested_quantity * Decimal(str(scale))
                            ).quantize(tick)
                    except (TypeError, ValueError, InvalidOperation):
                        pass

        min_qty = Decimal(str(self.config.get("min_quantity", "0.0001")))
        if suggested_quantity < min_qty:
            suggested_quantity = min_qty

        md = dict(raw.metadata or {})
        self._enrich_metadata_with_net(md, net, news_score)
        md["signal_engine_sizing_path"] = sizing_path
        if last_price is not None and last_price > 0:
            md["signal_engine_resolved_notional"] = str(
                (suggested_quantity * last_price).quantize(Decimal("0.01"))
            )
        effective_news = float(net.score) if net is not None else news_score

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
            news_score=effective_news,
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
        now = datetime.now(timezone.utc)
        net, _ = self._apply_accumulator(raw, news_score=news_score, now=now)

        news_veto, adjusted_confidence = self._veto_and_confidence(
            raw,
            news_score=news_score,
            net=net,
            apply_news_overlay=not skip_news,
        )

        md = dict(raw.metadata or {})
        self._enrich_metadata_with_net(md, net, news_score)
        effective_news = float(net.score) if net is not None else news_score
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
            news_score=effective_news,
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
        now = datetime.now(timezone.utc)
        net, _ = self._apply_accumulator(raw, news_score=news_score, now=now)

        news_veto, adjusted_confidence = self._veto_and_confidence(
            raw,
            news_score=news_score,
            net=net,
            apply_news_overlay=True,
        )
        if news_veto:
            return None
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
        md = dict(raw.metadata or {})
        self._enrich_metadata_with_net(md, net, news_score)
        return SignalCandidate(
            symbol=raw.symbol,
            asset_class=cast(AssetClass, ac),
            side=side,
            timestamp=datetime.now(timezone.utc),
            raw_signal_strength=Decimal(str(raw.confidence)),
            adjusted_signal_strength=Decimal(str(adjusted_confidence)),
            confidence=Decimal(str(adjusted_confidence)),
            strategy_name=raw.strategy,
            metadata=md,
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
