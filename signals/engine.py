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
from signals.anti_churn import AntiChurnGate

if TYPE_CHECKING:
    from signals.accumulator import SignalAccumulator
    from signals.trained_meta_labeler import TrainedMetaLabelerConfig

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
        # D115 — anti-churn gate. Stops same-strategy duplicate signal storm,
        # cross-strategy long/short contradictions on the same symbol, and
        # post-fill re-entry churn. Reads ``signal_engine.anti_churn`` from
        # ``config/strategies.yaml``. All gates default ON; set
        # ``signal_engine.anti_churn.enabled: false`` to disable wholesale.
        anti_churn_cfg = config.get("anti_churn") or {}
        if isinstance(anti_churn_cfg, dict) and not anti_churn_cfg.get("enabled", True):
            self.anti_churn: Optional[AntiChurnGate] = None
        else:
            self.anti_churn = AntiChurnGate(
                anti_churn_cfg if isinstance(anti_churn_cfg, dict) else None
            )
        # Wave 2 — trained meta-labeller. Default OFF (heuristic in
        # ``signals/meta_labeler.py`` remains the live filter). When
        # ``signal_engine.use_trained_meta_labeler`` is True we lazy-load
        # the YAML config; the runtime hook itself enforces the
        # registry/approval gates and falls back safely if no model is
        # registered.
        self._trained_meta_cfg: Optional["TrainedMetaLabelerConfig"] = None
        if bool(self.config.get("use_trained_meta_labeler", False)):
            from signals.trained_meta_labeler import TrainedMetaLabelerConfig

            try:
                self._trained_meta_cfg = TrainedMetaLabelerConfig.load()
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "signal_engine | trained_meta_labeler config load failed (%s) — disabling",
                    exc,
                )
                self._trained_meta_cfg = None

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

    def _apply_trained_meta_label(
        self,
        raw: RawSignal,
        *,
        adjusted_confidence: float,
        news_score: Optional[float],
        net: Optional[NetSignal],
        md: dict,
    ) -> bool:
        """
        Wave 2 — score the candidate through the trained meta-labeller.

        Returns ``True`` if the signal should proceed (or the labeller is
        disabled / passing through), ``False`` if it must be skipped.
        Either way, decision metadata is attached to ``md`` so the
        dashboard funnel can render the reason.
        """
        cfg = self._trained_meta_cfg
        if cfg is None:
            return True

        # Build a flat feature dict. The artefact reorders to its own
        # contract; missing keys default to 0.0 inside ``evaluate_features``.
        side_sign = 1.0 if (raw.side or "").lower() in ("buy", "long") else -1.0
        features: dict[str, float] = {
            "strategy_confidence": float(adjusted_confidence),
            "raw_confidence": float(raw.confidence),
            "side_sign": side_sign,
            "news_score": float(news_score) if news_score is not None else 0.0,
        }
        if net is not None:
            try:
                features["accumulator_score"] = float(net.score)
                features["accumulator_confidence"] = float(net.confidence)
            except (TypeError, ValueError):
                pass
        # Promote any pre-numeric metadata keys (volume_z, atr_pct, etc.)
        # so the artefact can use them without a separate lookup.
        for k, v in (raw.metadata or {}).items():
            if k in features:
                continue
            try:
                features[k] = float(v)
            except (TypeError, ValueError):
                continue

        # Mode is determined at runtime; default to PAPER so research
        # models never sneak into live without a deliberate flip.
        from signals.trained_meta_labeler import evaluate_features
        from models.schemas import Mode

        # Extract dynamic context for the threshold resolver. These keys
        # are stamped on the candidate metadata by upstream (opportunity
        # engine / coordinator / trading loop). Missing fields fall back
        # to neutral values, so behaviour degrades gracefully when the
        # heartbeat hasn't yet published a regime snapshot.
        def _ctx_float(key: str, default: float) -> float:
            try:
                v = md.get(key)
                if v is None:
                    return default
                return float(v)
            except (TypeError, ValueError):
                return default

        try:
            decision = evaluate_features(
                features=features,
                mode=Mode.PAPER,
                config=cfg,
                regime=md.get("regime_label"),
                portfolio_mode=md.get("profile_mode"),
                market_state_score=_ctx_float("market_state_score", 1.0),
                market_volatility_scalar=_ctx_float("market_volatility_scalar", 1.0),
                deployment_pressure=_ctx_float("deployment_pressure", 0.0),
            )
        except Exception as exc:  # noqa: BLE001
            # Defensive: never let the meta-labeller take down signal
            # generation. Surface the failure as a metadata note and
            # pass through.
            logger.warning("trained_meta_labeler | evaluate failed: %s", exc)
            md["meta_label_error"] = str(exc)
            return True

        md["meta_label_probability"] = (
            None if decision.probability is None else float(decision.probability)
        )
        md["meta_label_threshold"] = float(decision.threshold)
        md["meta_label_reason"] = decision.reason
        md["meta_label_kept"] = bool(decision.kept)
        if decision.model_name:
            md["meta_label_model_name"] = decision.model_name
        if decision.model_version:
            md["meta_label_model_version"] = decision.model_version
        if decision.feature_hash:
            md["meta_label_feature_hash"] = decision.feature_hash

        return bool(decision.kept)

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

    def _get_min_kelly_trades(self) -> int:
        try:
            from control.runtime import get_risk_engine
            re = get_risk_engine()
            if re is not None and hasattr(re, "_parameters"):
                return int(re._parameters.get_value("min_kelly_trades"))
        except Exception:
            pass
        try:
            from risk.parameters import ParameterManager
            pm = ParameterManager()
            return int(pm.get_value("min_kelly_trades"))
        except Exception:
            return 30  # absolute fallback

    def _get_strategy_stats_sync(self, strategy: str) -> tuple[float, float, int]:
        """Query strategy historical performance from the database synchronously."""
        import asyncio
        import concurrent.futures
        from sqlalchemy import select
        from storage.db import get_session_factory
        from storage.models import FillLog

        session_factory = get_session_factory()
        if session_factory is None:
            # Fallback values from config
            k_cfg = self.config.get("kelly_sizing") or {}
            fallback_wr = float(k_cfg.get("fallback_win_rate", 0.50))
            fallback_wlr = float(k_cfg.get("fallback_win_loss_ratio", 1.0))
            return fallback_wr, fallback_wlr, 0

        async def _query():
            async with session_factory() as session:
                stmt = (
                    select(FillLog)
                    .where(FillLog.strategy == strategy, FillLog.realised_pnl != 0)
                    .order_by(FillLog.timestamp.desc())
                    .limit(100)
                )
                res = await session.execute(stmt)
                fills = list(res.scalars().all())
                if not fills:
                    k_cfg = self.config.get("kelly_sizing") or {}
                    fallback_wr = float(k_cfg.get("fallback_win_rate", 0.50))
                    fallback_wlr = float(k_cfg.get("fallback_win_loss_ratio", 1.0))
                    return fallback_wr, fallback_wlr, 0

                wins = [f for f in fills if f.realised_pnl > 0]
                losses = [f for f in fills if f.realised_pnl < 0]
                win_rate = len(wins) / len(fills)

                avg_win = float(sum(f.realised_pnl for f in wins) / len(wins)) if wins else 0.0
                avg_loss = float(sum(abs(f.realised_pnl) for f in losses) / len(losses)) if losses else 0.0
                win_loss_ratio = avg_win / avg_loss if avg_loss > 0 else 1.0
                if win_loss_ratio <= 0:
                    win_loss_ratio = 1.0
                return win_rate, win_loss_ratio, len(fills)

        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                    future = executor.submit(lambda: asyncio.run(_query()))
                    return future.result()
            else:
                return loop.run_until_complete(_query())
        except Exception as exc:
            logger.debug("signal_engine | kelly stats query failed, using fallback: %s", exc)
            k_cfg = self.config.get("kelly_sizing") or {}
            fallback_wr = float(k_cfg.get("fallback_win_rate", 0.50))
            fallback_wlr = float(k_cfg.get("fallback_win_loss_ratio", 1.0))
            return fallback_wr, fallback_wlr, 0

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
        raw_md = raw.metadata or {}
        is_operator_close = bool(
            raw_md.get("reduce_only")
            or raw_md.get("close_only")
            or raw_md.get("flatten_all")
            or str(raw_md.get("coordinator_kind", "")).lower() == "trim_symbol"
        )
        is_allocator_selected = bool(raw_md.get("allocation_selected")) or (
            str(raw_md.get("coordinator_kind", "")).lower() == "open_strategy"
        )
        if is_operator_close or is_allocator_selected:
            # Operator/runtime exits and allocator-selected opens are already
            # post-selection intents. Do not run the same AI/meta gates twice;
            # risk still has unconditional say before execution.
            net = None
            news_veto = False
            adjusted_confidence = float(raw.confidence)
        else:
            net, _ = self._apply_accumulator(raw, news_score=news_score, now=now)

            news_veto, adjusted_confidence = self._veto_and_confidence(
                raw,
                news_score=news_score,
                net=net,
                apply_news_overlay=True,
            )

            # D115 anti-churn gate: dedup / cross-strategy / post-fill cooldown.
            # Only applied to natural strategy signals; operator closes and
            # allocator-selected opens above are exempt.
            ac_block = self._anti_churn_check(
                raw,
                adjusted_confidence=adjusted_confidence,
                now=now,
            )
            if ac_block is not None:
                return None

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
            # Phase 3 adaptive path: vol-targeted sizing via
            # :mod:`system.adaptive_sizing`. Replaces the static
            # ``default_position_pct = 0.05`` (5% of NAV on everything)
            # with a risk-budget-driven sizer: ``notional = NAV ×
            # risk_per_trade / atr_pct``. Every trade now risks the same
            # dollar amount in a 1-ATR adverse move, regardless of
            # symbol. The static ``default_position_pct`` becomes the
            # safety fallback when ``atr_pct`` is missing.
            #
            # Operator opt-out: ``volatility_sizing.enabled = False`` in
            # YAML forces the legacy path (preserves the pre-Phase-3
            # contract for anyone relying on it). The default (missing
            position_pct = self.config.get("default_position_pct", 0.05)
            vs_cfg = self.config.get("volatility_sizing") or {}
            adaptive_enabled = bool(vs_cfg.get("enabled", True)) if isinstance(vs_cfg, dict) else True
            kelly_cfg = self.config.get("kelly_sizing") or {}
            use_kelly = bool(kelly_cfg.get("enabled", False)) if isinstance(kelly_cfg, dict) else False
            kelly_fraction = float(kelly_cfg.get("kelly_fraction", 0.25)) if isinstance(kelly_cfg, dict) else 0.25

            if not adaptive_enabled and not use_kelly:
                # Legacy path explicitly requested — preserve old behaviour.
                suggested_quantity = self._calculate_quantity(
                    portfolio_value,
                    position_pct,
                    raw.symbol,
                    last_price=last_price,
                )
                sizing_path = "nav_fallback"
            else:
                sizing_path = "nav_fallback"
                try:
                    from system.adaptive_sizing import SizingInputs, compute_position_size
                    atr_pct_raw = raw_md.get("atr_pct")
                    try:
                        atr_pct_val = float(atr_pct_raw) if atr_pct_raw is not None else None
                    except (TypeError, ValueError):
                        atr_pct_val = None
                    mode_str = str(raw_md.get("profile_mode") or self.config.get("_active_profile_mode") or "hunter")

                    win_rate: float | None = None
                    win_loss_ratio: float | None = None
                    fill_count = 0
                    if use_kelly:
                        win_rate, win_loss_ratio, fill_count = self._get_strategy_stats_sync(raw.strategy)

                    min_trades = self._get_min_kelly_trades()
                    use_kelly_effective = use_kelly and (fill_count >= min_trades)
                    if use_kelly and not use_kelly_effective:
                        logger.info("signals | kelly sample size below minimum (%d < %d): falling back to vol-adjusted sizing", fill_count, min_trades)

                    decision = compute_position_size(
                        SizingInputs(
                            nav=portfolio_value,
                            last_price=last_price,
                            atr_pct=atr_pct_val,
                            mode=mode_str,
                            fallback_position_pct=float(position_pct),
                            confidence=float(raw.confidence),
                            win_rate=win_rate,
                            win_loss_ratio=win_loss_ratio,
                            kelly_fraction=kelly_fraction,
                            use_kelly=use_kelly_effective,
                        )
                    )
                    if decision.path == "kelly_negative_edge_drop":
                        logger.info("signals | kelly negative edge or zero: dropping signal %s", raw.symbol)
                        return None

                    suggested_quantity = decision.quantity.quantize(tick) if decision.quantity > 0 else Decimal("0")
                    if suggested_quantity > 0:
                        sizing_path = f"adaptive_sizing:{decision.path}"
                    else:
                        # Adaptive sizer couldn't produce a tradable size (no
                        # last_price), fall through to the legacy path so we
                        # still get a notional-denominated number.
                        suggested_quantity = self._calculate_quantity(
                            portfolio_value,
                            position_pct,
                            raw.symbol,
                            last_price=last_price,
                        )
                except Exception as exc:  # noqa: BLE001
                    logger.debug("adaptive_sizing | failed, falling back to legacy: %s", exc)
                    suggested_quantity = self._calculate_quantity(
                        portfolio_value,
                        position_pct,
                        raw.symbol,
                        last_price=last_price,
                    )

        min_qty = Decimal(str(self.config.get("min_quantity", "0.0001")))
        if suggested_quantity < min_qty:
            suggested_quantity = min_qty

        md = dict(raw.metadata or {})
        self._enrich_metadata_with_net(md, net, news_score)
        if not (is_operator_close or is_allocator_selected) and not self._apply_trained_meta_label(
            raw,
            adjusted_confidence=adjusted_confidence,
            news_score=news_score,
            net=net,
            md=md,
        ):
            logger.info(
                "Signal SKIPPED meta_label | %s %s | reason=%s prob=%s thr=%s",
                raw.symbol,
                raw.side,
                md.get("meta_label_reason"),
                md.get("meta_label_probability"),
                md.get("meta_label_threshold"),
            )
            return None
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

        if not news_veto and not (is_operator_close or is_allocator_selected):
            self._anti_churn_record_signal(
                raw,
                adjusted_confidence=adjusted_confidence,
                suggested_price=last_price,
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
        *,
        profile_mode: Optional[str] = None,
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
            _md = dict(raw.metadata or {})
            _md["_filter_reason"] = "news_veto"
            raw.metadata = _md
            return None
        # D115 anti-churn gate (D015 batch path).
        ac_reason = self._anti_churn_check(raw, adjusted_confidence=adjusted_confidence, now=now)
        if ac_reason is not None:
            _md = dict(raw.metadata or {})
            _md["_filter_reason"] = f"anti_churn:{ac_reason}"
            raw.metadata = _md
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
        if profile_mode:
            md["profile_mode"] = str(profile_mode)
        self._enrich_metadata_with_net(md, net, news_score)
        if not self._apply_trained_meta_label(
            raw,
            adjusted_confidence=adjusted_confidence,
            news_score=news_score,
            net=net,
            md=md,
        ):
            logger.info(
                "SignalCandidate SKIPPED meta_label | %s %s | reason=%s prob=%s thr=%s",
                raw.symbol,
                raw.side,
                md.get("meta_label_reason"),
                md.get("meta_label_probability"),
                md.get("meta_label_threshold"),
            )
            md["_filter_reason"] = f"meta_label_below_threshold:{md.get('meta_label_reason') or 'low_prob'}"
            raw.metadata = md
            return None
        candidate = SignalCandidate(
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
        # D115 — record after a successful build so the gate can dedup later
        # identical candidates and detect contradictions on this symbol.
        self._anti_churn_record_signal(
            raw,
            adjusted_confidence=adjusted_confidence,
            suggested_price=self._extract_last_price(md),
        )
        return candidate

    # ---------------- D115 anti-churn helpers ----------------
    def _anti_churn_check(
        self,
        raw: "RawSignal",
        *,
        adjusted_confidence: float,
        now: datetime,
    ) -> Optional[str]:
        """Run the anti-churn gate. Returns block reason or None."""
        if self.anti_churn is None:
            return None
        try:
            last_price = self._extract_last_price(raw.metadata or {})
            price_float: Optional[float] = float(last_price) if last_price is not None else None
            md = raw.metadata or {}
            profile_mode = str(md.get("profile_mode") or "hunter")
            # D141 — pass live regime + fill-rate context so the gate's
            # cooldown is computed dynamically.
            try:
                mss = float(md.get("market_state_score") or 0)
            except (TypeError, ValueError):
                mss = 0.0
            try:
                fill_rate = float(md.get("recent_fill_rate_per_min") or 0)
            except (TypeError, ValueError):
                fill_rate = 0.0
            decision = self.anti_churn.check(
                strategy=str(raw.strategy or ""),
                symbol=str(raw.symbol or ""),
                side=raw.side,
                confidence=float(adjusted_confidence),
                suggested_price=price_float,
                broker=str(raw.broker or ""),
                profile_mode=profile_mode,
                now=now,
                market_state_score=mss,
                recent_fill_rate_per_min=fill_rate,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("anti_churn | check failed (passing through): %s", exc)
            return None
        if decision.allow:
            return None
        # Annotate raw.metadata so candidate-funnel logs can surface the
        # block reason in the dashboard.
        try:
            md = raw.metadata if isinstance(raw.metadata, dict) else {}
            md["anti_churn_blocked"] = True
            md["anti_churn_reason"] = decision.reason
            md["anti_churn_details"] = decision.details
            raw.metadata = md
        except Exception:  # noqa: BLE001
            pass
        logger.info(
            "anti_churn | BLOCKED %s %s %s conf=%.3f reason=%s",
            raw.strategy,
            raw.symbol,
            raw.side,
            float(adjusted_confidence),
            decision.reason,
        )
        return decision.reason

    def _anti_churn_record_signal(
        self,
        raw: "RawSignal",
        *,
        adjusted_confidence: float,
        suggested_price: Optional[Decimal],
    ) -> None:
        if self.anti_churn is None:
            return
        try:
            price_float: Optional[float] = float(suggested_price) if suggested_price is not None else None
            self.anti_churn.record_signal(
                strategy=str(raw.strategy or ""),
                symbol=str(raw.symbol or ""),
                side=raw.side,
                confidence=float(adjusted_confidence),
                suggested_price=price_float,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("anti_churn | record_signal failed: %s", exc)

    def record_fill(
        self,
        *,
        broker: str,
        symbol: str,
        side: str,
        is_reduce_only: bool = False,
    ) -> None:
        """Trading loop calls this after every filled order so the gate's
        post-fill cooldown can start. Safe no-op when the gate is disabled."""
        if self.anti_churn is None:
            return
        try:
            self.anti_churn.record_fill(
                broker=str(broker or ""),
                symbol=str(symbol or ""),
                side=side,
                is_reduce_only=bool(is_reduce_only),
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("anti_churn | record_fill failed: %s", exc)

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
