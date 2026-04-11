"""
system/trading_loop.py
======================
Wraps the M5 trading loop as a controllable async task that the orchestrator
can start and stop on demand.  Reuses all existing strategy/risk/execution logic.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import yaml
from loguru import logger
from sqlalchemy import select

from ai.news_classifier import NewsClassifier
from ai.pipeline import AIPipeline
from ai.router import AIRouter
from ai.regime import filter_by_allowed_strategies
from ai.thesis_generator import ThesisGenerator
from control.command_bus import CommandBus
from control.runner_control import (
    apply_control_commands,
    hydrate_risk_parameters_from_bus,
    publish_runner_heartbeat,
)
from data.scanner import UniverseScanner
from data.universe import UniverseManager
from data.universe_tiers import load_universe_tiers
from config.loaders import load_allocation, load_profile_modes
from core.models_runtime import AssetClass
from control.runtime import set_risk_engine
from execution.d015_instruction_executor import risk_signal_from_execution_instruction
from execution.engine import ExecutionEngine
from execution.planner import build_execution_plan
from execution.router import SmartOrderRouter
from graph.engine import DependencyGraphEngine
from graph.pipeline import DiscoveryPipeline
from portfolio.allocation_engine import build_allocation_decision
from portfolio.d015_smoothing import allocation_smoothing_snapshot, apply_allocation_smoothing
from risk.engine import RiskEngine, RiskVerdict
from risk.regime_state import compute_regime_state_async
from signals.opportunity_engine import build_opportunities_async
from signals.engine import unified_signal_to_signal_candidate
from system.d015_escalation import (
    drain_volume_refresh_features,
    enqueue_volume_escalation_symbols,
    load_replacement_context_from_bus,
    load_smoothing_prev_from_bus,
    merge_replacement_events_from_decision,
    save_replacement_context_to_bus,
    save_smoothing_prev_to_bus,
)
from system.d015_portfolio_bridge import portfolio_dict_to_runtime_state
from system.d015_shadow import log_d015_shadow_for_signal
from system.portfolio_equity import live_portfolio_value
from risk.m8_loader import merge_m8_into_risk_cfg
from run_m3 import (
    _apply_signal_to_portfolio_state,
    _load_portfolio_state,
    _load_recent_features,
    _persist_position_snapshot,
    _persist_signal,
    _pick_best_signal,
    _upsert_daily_pnl,
)
from signals.engine import SignalEngine
from storage.db import dispose_engine, init_async_database
from storage.discovery import persist_anomaly_log, persist_thesis_log
from storage.models import AIOutputLog, FeatureSnapshot
from strategies.mean_reversion import MeanReversionStrategy
from strategies.momentum import MomentumBreakoutStrategy


_CRYPTO_SUFFIXES = ("-USD", "-USDT", "-EUR", "-GBP", "/USD", "/USDT", "/EUR", "/GBP")
_CRYPTO_BASES = {"BTC", "ETH", "SOL", "XRP", "DOGE", "ADA", "AVAX", "DOT", "MATIC", "LINK", "UNI", "LTC"}


def _is_crypto_symbol(symbol: str) -> bool:
    s = symbol.upper().strip()
    if any(s.endswith(suf) for suf in _CRYPTO_SUFFIXES):
        base = s.split("-")[0].split("/")[0]
        if base in _CRYPTO_BASES:
            return True
    return False


def _load_yaml(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    if not p.exists():
        return {}
    with p.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _enrich_signal_volume_z(signal: Any, df: Any) -> None:
    """
    Compute rolling volume z-score from the feature DataFrame and store in
    signal.metadata["volume_z_score"].  Used by the quality gate in the risk engine.
    Safe — never raises; leaves metadata unchanged on any error.
    """
    try:
        import pandas as pd  # already available in this module's env
        if df is None or not hasattr(df, "empty") or df.empty:
            return
        if "volume" not in df.columns:
            return
        vol = df["volume"].dropna()
        if len(vol) < 5:
            return
        mean_v = float(vol.mean())
        std_v = float(vol.std())
        if std_v <= 0:
            return
        latest_v = float(vol.iloc[-1])
        z = (latest_v - mean_v) / std_v
        # Ensure metadata is a real dict (not None) before writing
        if not isinstance(getattr(signal, "metadata", None), dict):
            signal.metadata = {}
        signal.metadata["volume_z_score"] = round(z, 4)
    except Exception:  # noqa: BLE001
        pass


def _apply_saved_mode_to_risk_cfg(risk_engine: "RiskEngine") -> None:
    """Apply the last-saved mode profile to the risk engine config dict at startup."""
    import json as _json

    mode_file = Path("data/runtime/active_mode.json")
    if not mode_file.is_file():
        return
    try:
        mode = _json.loads(mode_file.read_text(encoding="utf-8")).get("mode", "trader")
    except Exception:  # noqa: BLE001
        return
    modes = _load_yaml("config/risk_modes.yaml")
    profile = modes.get(mode, {})
    if risk_engine.config.get("allocator_d015_primary"):
        for key in ("label", "description"):
            if key in profile:
                risk_engine.config[key] = profile[key]
        if profile:
            logger.info("trading_loop | applied mode labels only (D015 primary) | mode={}", mode)
        return
    for key, value in profile.items():
        if key in ("label", "description"):
            continue
        risk_engine.config[key] = value
    if profile:
        logger.info("trading_loop | applied mode profile | mode={}", mode)


def _d015_legacy_fallback() -> bool:
    return os.getenv("ALLOCATOR_D015_LEGACY_FALLBACK", "").strip().lower() in ("1", "true", "yes", "on")


def _enrich_candidate_volume_z(candidate: Any, df: Any) -> None:
    try:
        import pandas as pd  # noqa: F401

        if df is None or not hasattr(df, "empty") or df.empty:
            return
        if "volume" not in df.columns:
            return
        vol = df["volume"].dropna()
        if len(vol) < 5:
            return
        mean_v = float(vol.mean())
        std_v = float(vol.std())
        if std_v <= 0:
            return
        latest_v = float(vol.iloc[-1])
        z = (latest_v - mean_v) / std_v
        if not isinstance(getattr(candidate, "metadata", None), dict):
            candidate.metadata = {}
        candidate.metadata["volume_z_score"] = round(z, 4)
    except Exception:  # noqa: BLE001
        pass


class TradingLoop:
    """
    A controllable trading loop that can be started/stopped by the orchestrator.
    Wraps all M5 logic without requiring CLI args.
    """

    def __init__(
        self,
        broker_configs: dict[str, dict[str, Any]],
        available_brokers: list[str],
        paper_mode: bool = True,
        *,
        portfolio_value: float = 100_000.0,
        loop_interval_sec: int = 120,
        timeframe: str = "1h",
        lookback_bars: int = 200,
        reconcile_interval_sec: int = 300,
        broker_manager: Any = None,
        capital_pct: float = 1.0,
    ):
        self.broker_configs = broker_configs
        self.available_brokers = list(available_brokers)
        self.paper_mode = paper_mode
        self._broker_manager = broker_manager
        self.portfolio_value = portfolio_value
        self.capital_pct = max(0.0, min(1.0, capital_pct))
        self.loop_interval_sec = max(10, loop_interval_sec)
        self.timeframe = timeframe
        self.lookback_bars = lookback_bars
        self.reconcile_interval_sec = reconcile_interval_sec

        self._task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()
        self._running = False

        self.risk_engine: RiskEngine | None = None
        self.execution_engine: ExecutionEngine | None = None
        self.sig_engine: SignalEngine | None = None
        self.router: SmartOrderRouter | None = None
        self.last_iteration_at: datetime | None = None
        self.iterations: int = 0
        self.last_error: str | None = None

    @property
    def is_running(self) -> bool:
        return self._running and self._task is not None and not self._task.done()

    def _check_late_brokers(self) -> None:
        """Pick up brokers that connected after startup (e.g. IBKR background connect)."""
        if self._broker_manager is None:
            return
        # Snapshot the dict to avoid "dictionary changed size during iteration"
        # when a broker connects asynchronously while we're looping.
        for name, adapter in list(self._broker_manager.adapters.items()):
            if name not in self.available_brokers:
                self.available_brokers.append(name)
                if self.router is not None:
                    self.router.add_broker(name)
                logger.info("trading_loop | late broker joined: {}", name)

    async def start(self) -> None:
        if self.is_running:
            logger.warning("trading_loop | already running")
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run(), name="trading-loop")
        logger.info("trading_loop | started")

    async def stop(self) -> None:
        if not self.is_running:
            return
        logger.info("trading_loop | stopping...")
        self._stop_event.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        self._running = False
        logger.info("trading_loop | stopped")

    async def _run(self) -> None:
        self._running = True
        engine = None
        try:
            strategies_cfg = _load_yaml("config/strategies.yaml")
            pipeline_cfg = _load_yaml("config/data_pipeline.yaml")
            risk_cfg = _load_yaml("config/risk_limits.yaml")
            merge_m8_into_risk_cfg(risk_cfg, "config/m8_micro_live.yaml")
            legacy_fb = _d015_legacy_fallback()
            if legacy_fb:
                risk_cfg["allocator_d015_primary"] = False
                risk_cfg["allocator_d015_enabled"] = (
                    os.getenv("ALLOCATOR_D015_ENABLED", "").strip().lower() in ("1", "true", "yes", "on")
                )
            else:
                risk_cfg["allocator_d015_enabled"] = True
                risk_cfg["allocator_d015_primary"] = True
            ai_cfg = _load_yaml("config/ai.yaml")
            discovery_cfg = _load_yaml("config/discovery.yaml")

            symbols_raw = pipeline_cfg.get("symbols", [])
            symbols = [s.strip() for s in symbols_raw if s.strip()] if isinstance(symbols_raw, list) else []
            # Keep base symbols permanently in the loop even in dynamic mode so
            # we always monitor a stable liquid anchor set (SPY/QQQ/BTC/ETH, etc).
            base_symbols = list(dict.fromkeys(symbols))
            if not symbols:
                logger.warning("trading_loop | no symbols in config/data_pipeline.yaml — idle")
                self._running = False
                return

            engine, session_factory = await init_async_database()
            if session_factory is None:
                logger.error("trading_loop | database unavailable — cannot run")
                self.last_error = "Database unavailable"
                self._running = False
                return

            self.router = SmartOrderRouter(list(self.available_brokers))
            self.execution_engine = ExecutionEngine(
                broker_configs=self.broker_configs,
                paper_mode=self.paper_mode,
                allowed_brokers=list(self.available_brokers),
            )
            self.sig_engine = SignalEngine(strategies_cfg.get("signal_engine", {}))
            self.risk_engine = RiskEngine(risk_cfg)
            # Starting the system from OFF should begin from a clean trading state.
            # Clear any stale latched kill switch from prior runs.
            try:
                self.risk_engine.reset_kill()
            except Exception:  # noqa: BLE001
                pass
            # Apply persisted mode overrides (if user selected a mode before this start)
            _apply_saved_mode_to_risk_cfg(self.risk_engine)
            set_risk_engine(self.risk_engine)
            discovery_enabled = bool(discovery_cfg.get("enabled", False)) and os.getenv("DISCOVERY_ENABLED", "1").strip().lower() not in {"0", "false", "no", "off"}
            discovery_pipeline = None
            if discovery_enabled:
                universe = UniverseManager()
                scanner = UniverseScanner(
                    universe,
                    session_factory,
                    cooldown_seconds=int(discovery_cfg.get("scanner", {}).get("cooldown_seconds", 300)),
                )
                graph = DependencyGraphEngine(relationships_path=str(discovery_cfg.get("relationships_path", "graph/data/relationships.yaml")))
                thesis_generator = ThesisGenerator()
                discovery_pipeline = DiscoveryPipeline(scanner, graph, thesis_generator, self.sig_engine)

            ai_enabled = bool(ai_cfg.get("enabled", True))
            ai_mode = str(ai_cfg.get("mode", "local_first")).strip().lower()
            if ai_enabled and ai_mode != "api_only":
                ai_classifier = AIRouter(ai_cfg)
            elif ai_enabled:
                ai_classifier = NewsClassifier()
            else:
                ai_classifier = None
            ai_pipeline = AIPipeline(ai_cfg.get("pipeline", {}), classifier=ai_classifier) if ai_enabled else None
            ai_cycle_timeout_sec = float(ai_cfg.get("pipeline", {}).get("cycle_timeout_seconds", 45))

            strat_cfg = strategies_cfg.get("strategies", {})
            momentum = MomentumBreakoutStrategy(strat_cfg.get("momentum_breakout", {}))
            mean_rev = MeanReversionStrategy(strat_cfg.get("mean_reversion", {}))
            strategies = {momentum.name: momentum, mean_rev.name: mean_rev}

            bus = CommandBus(session_factory)
            alloc_cfg = load_allocation()
            profile_modes_cfg = load_profile_modes()
            for name, strategy in strategies.items():
                state_v = await bus.get_state(f"strategy.enabled.{name}", None)
                if state_v is not None:
                    strategy.enabled = bool(state_v)
            await hydrate_risk_parameters_from_bus(bus, self.risk_engine)

            next_reconcile_at = datetime.now(timezone.utc).timestamp()
            universe_mode = str(pipeline_cfg.get("universe_mode", "static")).strip().lower()
            dynamic_cfg = pipeline_cfg.get("dynamic_universe", {}) or {}
            rank_cfg = dynamic_cfg.get("ranking", {}) or {}
            ranking_on = universe_mode == "dynamic" and bool(rank_cfg.get("enabled", False))
            tiers_path = Path(str(rank_cfg.get("tiers_path", "data/runtime/universe_tiers.json")).strip() or "data/runtime/universe_tiers.json")
            scan_strategy_every_n = max(1, int(rank_cfg.get("scan_strategy_every_n", 1)))
            scan_batch_size = max(1, int(rank_cfg.get("scan_batch_size", 45)))
            max_symbols_per_iteration = max(10, int(rank_cfg.get("max_symbols_per_iteration", 120)))
            db_symbol_cache: list[str] = []

            async def _refresh_symbols_from_db(limit: int = 300) -> list[str]:
                async with session_factory() as session:
                    q = await session.execute(
                        select(FeatureSnapshot.symbol).distinct().order_by(FeatureSnapshot.symbol.asc()).limit(limit)
                    )
                    rows = [str(r[0]).strip() for r in q.all() if r and str(r[0]).strip()]
                return rows

            def _symbols_for_tiered_iteration(
                tiered,
                db_syms: list[str],
                iteration: int,
            ) -> list[str] | None:
                if tiered is None:
                    return None
                by_upper: dict[str, str] = {}
                for s in db_syms:
                    u = s.strip().upper()
                    if u and u not in by_upper:
                        by_upper[u] = s.strip()
                core = list(tiered.core)
                scan = list(tiered.scan)
                want = list(core)
                if scan and iteration % scan_strategy_every_n == 0:
                    # Rotate through scan tier in small batches so each cycle stays responsive.
                    cycle_idx = iteration // scan_strategy_every_n
                    start = (cycle_idx * scan_batch_size) % len(scan)
                    batch = [scan[(start + j) % len(scan)] for j in range(min(scan_batch_size, len(scan)))]
                    want.extend(batch)
                ordered = [by_upper[s] for s in want if s in by_upper]
                if len(ordered) > max_symbols_per_iteration:
                    ordered = ordered[:max_symbols_per_iteration]
                return ordered if ordered else None

            async def _process_signal(signal, *, symbol_hint: str | None = None) -> bool:
                routed = self.router.route(signal.asset_class, signal.symbol)
                if routed is None:
                    return False
                signal.broker = routed
                portfolio_state = await _load_portfolio_state(
                    session_factory,
                    fallback_portfolio_value=total_equity,
                    signal_price_fallback=signal.suggested_price,
                    capital_pct=Decimal(str(self.capital_pct)),
                )
                self.risk_engine.update_high_watermark(
                    Decimal(str(portfolio_state.get("high_watermark_value", total_equity)))
                )
                self.risk_engine.restore_runtime_state(portfolio_state)
                risk_decision = await self.risk_engine.evaluate_and_persist(
                    session_factory, signal, portfolio_state,
                )
                if risk_decision.verdict != RiskVerdict.APPROVED:
                    await _persist_signal(
                        session_factory, signal,
                        paper_mode=self.paper_mode,
                        timeframe=self.timeframe,
                        feature_ts=datetime.now(timezone.utc),
                    )
                    return False
                await _persist_signal(
                    session_factory, signal,
                    paper_mode=self.paper_mode,
                    timeframe=self.timeframe,
                    feature_ts=datetime.now(timezone.utc),
                )
                result = await self.execution_engine.execute(
                    signal, risk_decision, session_factory=session_factory,
                )
                if result is None:
                    return False
                status_val = str(getattr(getattr(result, "status", None), "value", getattr(result, "status", ""))).lower()
                if status_val != "filled":
                    # Only treat fully filled orders as executed positions.
                    return False
                filled_qty = Decimal(str(getattr(result, "filled_quantity", "0") or "0"))
                if filled_qty <= 0:
                    return False
                # Refresh portfolio state after execution so persisted position/PnL
                # reflect newly filled orders (not the pre-trade snapshot).
                post_trade_state = await _load_portfolio_state(
                    session_factory,
                    fallback_portfolio_value=total_equity,
                    signal_price_fallback=signal.suggested_price,
                    capital_pct=Decimal(str(self.capital_pct)),
                )
                signal.suggested_quantity = filled_qty
                avg_fill = getattr(result, "avg_fill_price", None)
                if avg_fill is not None:
                    try:
                        avg_fill_d = Decimal(str(avg_fill))
                        if avg_fill_d > 0:
                            signal.suggested_price = avg_fill_d
                    except Exception:  # noqa: BLE001
                        pass
                _apply_signal_to_portfolio_state(post_trade_state, signal)
                await _persist_position_snapshot(session_factory, post_trade_state)
                await _upsert_daily_pnl(session_factory, post_trade_state)
                return True

            while not self._stop_event.is_set():
                if self.iterations == 0:
                    logger.info("trading_loop | startup flush — first iteration running immediately")
                self._check_late_brokers()
                if universe_mode == "dynamic" and (self.iterations % 5 == 0):
                    try:
                        db_symbols = await _refresh_symbols_from_db(limit=500)
                        if db_symbols:
                            db_symbol_cache = db_symbols
                            if ranking_on:
                                tiered = load_universe_tiers(tiers_path)
                                picked = _symbols_for_tiered_iteration(tiered, db_symbols, self.iterations)
                                picked_or_db = picked if picked is not None else db_symbols
                                symbols = list(dict.fromkeys(base_symbols + picked_or_db))
                            else:
                                symbols = list(dict.fromkeys(base_symbols + db_symbols))
                    except Exception as exc:
                        logger.debug("trading_loop | dynamic symbols refresh failed: {}", exc)
                elif universe_mode == "dynamic" and ranking_on and db_symbol_cache:
                    tiered = load_universe_tiers(tiers_path)
                    picked = _symbols_for_tiered_iteration(tiered, db_symbol_cache, self.iterations)
                    if picked is not None:
                        symbols = list(dict.fromkeys(base_symbols + picked))
                total_equity = await live_portfolio_value(self._broker_manager)
                if total_equity <= 0:
                    total_equity = Decimal(str(self.portfolio_value))
                tradable = total_equity * Decimal(str(self.capital_pct))
                effective_value = float(tradable)
                try:
                    generated = 0
                    executed = 0
                    await apply_control_commands(
                        bus,
                        risk_engine=self.risk_engine,
                        execution_engine=self.execution_engine,
                        strategies=strategies,
                    )

                    ai_result = None
                    if ai_pipeline is not None:
                        try:
                            ai_result = await asyncio.wait_for(
                                ai_pipeline.compute(session_factory, symbols),
                                timeout=ai_cycle_timeout_sec,
                            )
                            await asyncio.wait_for(
                                ai_pipeline.persist(session_factory, ai_result),
                                timeout=max(10.0, ai_cycle_timeout_sec / 2),
                            )
                        except asyncio.TimeoutError:
                            logger.warning(
                                "trading_loop | AI pipeline timed out after {}s (continuing without AI this cycle)",
                                ai_cycle_timeout_sec,
                            )
                            ai_result = None
                        except Exception as exc:
                            logger.warning("trading_loop | AI pipeline error (continuing): {}", exc)

                    mode_raw = "trader"
                    try:
                        import json as _json
                        from pathlib import Path as _Path

                        _mf = _Path("data/runtime/active_mode.json")
                        if _mf.is_file():
                            mode_raw = str(_json.loads(_mf.read_text(encoding="utf-8")).get("mode", "trader"))
                    except Exception:  # noqa: BLE001
                        pass

                    async def _resolve_price_for_symbol(sym: str) -> Decimal:
                        df_p, _ = await _load_recent_features(
                            session_factory,
                            symbol=sym,
                            timeframe=self.timeframe,
                            lookback_bars=self.lookback_bars,
                        )
                        if df_p is None or not hasattr(df_p, "empty") or df_p.empty:
                            return Decimal("1")
                        col = "close" if "close" in df_p.columns else None
                        if col is None:
                            return Decimal("1")
                        try:
                            return Decimal(str(float(df_p[col].iloc[-1])))
                        except Exception:  # noqa: BLE001
                            return Decimal("1")

                    def _asset_class_lookup(sym: str, cands: list) -> str:
                        for c in cands:
                            if getattr(c, "symbol", None) == sym:
                                return str(getattr(c, "asset_class", "equity") or "equity")
                        return "equity"

                    use_legacy = legacy_fb

                    if use_legacy:
                        for symbol in symbols:
                            if self._stop_event.is_set():
                                break

                            df, feature_ts = await _load_recent_features(
                                session_factory,
                                symbol=symbol,
                                timeframe=self.timeframe,
                                lookback_bars=self.lookback_bars,
                            )
                            if df.empty:
                                continue

                            raw_candidates = []
                            m_sig = momentum.generate_signal(symbol, df)
                            if m_sig is not None:
                                raw_candidates.append(m_sig)
                            r_sig = mean_rev.generate_signal(symbol, df)
                            if r_sig is not None:
                                raw_candidates.append(r_sig)

                            if ai_result is not None and ai_pipeline is not None:
                                allowed = ai_pipeline.allowed_strategy_names(ai_result.macro_regime)
                                filtered = filter_by_allowed_strategies(raw_candidates, allowed)
                                raw_candidates = filtered

                            raw = _pick_best_signal(raw_candidates)
                            if raw is None:
                                continue

                            signal = self.sig_engine.process(
                                raw,
                                portfolio_value=Decimal(str(effective_value)),
                                news_score=(ai_result.news_scores.get(symbol) if ai_result else None),
                            )
                            if signal is None:
                                continue

                            if _is_crypto_symbol(symbol) and signal.asset_class != "crypto":
                                signal.asset_class = "crypto"

                            if ai_result is not None:
                                signal.metadata["ai_macro_regime"] = ai_result.macro_regime
                                signal.metadata["ai_macro_confidence"] = ai_result.macro_confidence

                            _enrich_signal_volume_z(signal, df)

                            await log_d015_shadow_for_signal(
                                session_factory,
                                symbol=signal.symbol,
                                strategy_name=signal.strategy,
                                asset_class=signal.asset_class,
                                side=signal.side,
                                confidence=float(signal.confidence),
                                adjusted_strength=Decimal(str(signal.confidence)),
                                news_score=(
                                    float(ai_result.news_scores.get(symbol))
                                    if ai_result and ai_result.news_scores.get(symbol) is not None
                                    else None
                                ),
                                metadata=dict(signal.metadata or {}),
                                universe_symbols=list(symbols),
                                nav_estimate=Decimal(str(total_equity)),
                                capital_pct=float(self.capital_pct),
                                mode=mode_raw,
                                timeframe=self.timeframe,
                                legacy_suggested_qty=signal.suggested_quantity,
                            )

                            generated += 1
                            ok = await _process_signal(signal, symbol_hint=symbol)
                            if ok:
                                executed += 1

                        if discovery_pipeline is not None:
                            discovery_items = await discovery_pipeline.run_cycle_detailed(
                                portfolio_value=Decimal(str(effective_value)),
                                market_context={
                                    "macro_regime": ai_result.macro_regime if ai_result is not None else None,
                                    "macro_confidence": ai_result.macro_confidence if ai_result is not None else None,
                                },
                            )
                            for item in discovery_items:
                                anomaly = item.anomaly
                                thesis = item.thesis
                                d_signals = item.signals
                                for ds in d_signals:
                                    if ai_result is not None:
                                        ds.news_score = ai_result.news_scores.get(ds.symbol)
                                        ds.metadata["ai_macro_regime"] = ai_result.macro_regime
                                        ds.metadata["ai_macro_confidence"] = ai_result.macro_confidence
                                        ds.metadata["ai_news_detail"] = ai_result.news_details.get(ds.symbol, {})
                                    if item.anomaly is not None:
                                        ds.metadata["anomaly_score"] = float(getattr(item.anomaly, "anomaly_score", 0) or 0)
                                        ds.metadata["price_z_score"] = float(getattr(item.anomaly, "price_z_score", 0) or 0)
                                    generated += 1
                                    ok = await _process_signal(ds, symbol_hint=ds.symbol)
                                    if ok:
                                        executed += 1
                                await persist_anomaly_log(
                                    session_factory,
                                    anomaly=anomaly,
                                    opportunities_found=len(item.opportunities),
                                    thesis_generated=thesis is not None,
                                    signals_produced=len(d_signals),
                                )
                                if thesis is not None:
                                    await persist_thesis_log(
                                        session_factory,
                                        thesis=thesis,
                                    )
                    else:
                        batch_candidates: list = []
                        for symbol in symbols:
                            if self._stop_event.is_set():
                                break

                            df, feature_ts = await _load_recent_features(
                                session_factory,
                                symbol=symbol,
                                timeframe=self.timeframe,
                                lookback_bars=self.lookback_bars,
                            )
                            if df.empty:
                                continue

                            raw_candidates = []
                            m_sig = momentum.generate_signal(symbol, df)
                            if m_sig is not None:
                                raw_candidates.append(m_sig)
                            r_sig = mean_rev.generate_signal(symbol, df)
                            if r_sig is not None:
                                raw_candidates.append(r_sig)

                            if ai_result is not None and ai_pipeline is not None:
                                allowed = ai_pipeline.allowed_strategy_names(ai_result.macro_regime)
                                raw_candidates = filter_by_allowed_strategies(raw_candidates, allowed)

                            raw = _pick_best_signal(raw_candidates)
                            if raw is None:
                                continue

                            cand = self.sig_engine.raw_to_signal_candidate(
                                raw,
                                news_score=(ai_result.news_scores.get(symbol) if ai_result else None),
                            )
                            if cand is None:
                                continue

                            if _is_crypto_symbol(symbol) and str(cand.asset_class) != "crypto":
                                cand.asset_class = cast(AssetClass, "crypto")

                            if ai_result is not None:
                                cand.metadata["ai_macro_regime"] = ai_result.macro_regime
                                cand.metadata["ai_macro_confidence"] = ai_result.macro_confidence

                            _enrich_candidate_volume_z(cand, df)
                            batch_candidates.append(cand)

                        if discovery_pipeline is not None:
                            discovery_items = await discovery_pipeline.run_cycle_detailed(
                                portfolio_value=Decimal(str(effective_value)),
                                market_context={
                                    "macro_regime": ai_result.macro_regime if ai_result is not None else None,
                                    "macro_confidence": ai_result.macro_confidence if ai_result is not None else None,
                                },
                            )
                            for item in discovery_items:
                                anomaly = item.anomaly
                                thesis = item.thesis
                                d_signals = item.signals
                                for ds in d_signals:
                                    if ai_result is not None:
                                        ds.news_score = ai_result.news_scores.get(ds.symbol)
                                        ds.metadata["ai_macro_regime"] = ai_result.macro_regime
                                        ds.metadata["ai_macro_confidence"] = ai_result.macro_confidence
                                        ds.metadata["ai_news_detail"] = ai_result.news_details.get(ds.symbol, {})
                                    if item.anomaly is not None:
                                        ds.metadata["anomaly_score"] = float(getattr(item.anomaly, "anomaly_score", 0) or 0)
                                        ds.metadata["price_z_score"] = float(getattr(item.anomaly, "price_z_score", 0) or 0)
                                    batch_candidates.append(unified_signal_to_signal_candidate(ds))
                                await persist_anomaly_log(
                                    session_factory,
                                    anomaly=anomaly,
                                    opportunities_found=len(item.opportunities),
                                    thesis_generated=thesis is not None,
                                    signals_produced=len(d_signals),
                                )
                                if thesis is not None:
                                    await persist_thesis_log(
                                        session_factory,
                                        thesis=thesis,
                                    )

                        generated = len(batch_candidates)
                        executed = 0
                        if batch_candidates:
                            portfolio_dict = await _load_portfolio_state(
                                session_factory,
                                fallback_portfolio_value=total_equity,
                                signal_price_fallback=None,
                                capital_pct=Decimal(str(self.capital_pct)),
                            )
                            ps_rt = portfolio_dict_to_runtime_state(
                                portfolio_dict,
                                mode=mode_raw,
                                capital_pct=float(self.capital_pct),
                            )
                            async with session_factory() as session:
                                feat_extra = await drain_volume_refresh_features(
                                    session,
                                    bus,
                                    universe_symbols=list(symbols),
                                    timeframe=self.timeframe,
                                    allocation_cfg=alloc_cfg,
                                )
                                regime = await compute_regime_state_async(
                                    portfolio_state=ps_rt,
                                    allocation_cfg=alloc_cfg,
                                    session=session,
                                    universe_symbols=list(symbols),
                                    timeframe=self.timeframe,
                                )
                                opps = await build_opportunities_async(
                                    signals=batch_candidates,
                                    regime_state=regime,
                                    allocation_cfg=alloc_cfg,
                                    session=session,
                                    timeframe=self.timeframe,
                                    profile_cfg=profile_modes_cfg,
                                    active_profile_mode=mode_raw
                                    if mode_raw in profile_modes_cfg.modes
                                    else profile_modes_cfg.defaults.active_mode,
                                    feature_json_by_symbol=feat_extra,
                                )
                            esc_syms = [o.symbol for o in opps if o.metadata.get("d015_escalate_context")]
                            await enqueue_volume_escalation_symbols(bus, esc_syms, alloc_cfg)
                            repl_ctx = await load_replacement_context_from_bus(bus)
                            dec = build_allocation_decision(
                                opportunities=opps,
                                portfolio_state=ps_rt,
                                regime_state=regime,
                                allocation_cfg=alloc_cfg,
                                profile_cfg=profile_modes_cfg,
                                replacement_context=repl_ctx,
                            )
                            smooth_prev = await load_smoothing_prev_from_bus(bus)
                            dec = apply_allocation_smoothing(
                                dec,
                                prev=smooth_prev,
                                stability_cfg=alloc_cfg.allocation_stability,
                                nav=ps_rt.nav,
                            )
                            merge_replacement_events_from_decision(
                                repl_ctx,
                                decision=dec,
                                now=datetime.now(timezone.utc),
                            )
                            await save_replacement_context_to_bus(bus, repl_ctx)
                            await save_smoothing_prev_to_bus(bus, allocation_smoothing_snapshot(dec))
                            plan = build_execution_plan(
                                decision=dec,
                                portfolio_state=ps_rt,
                                allocation_cfg=alloc_cfg,
                            )
                            logger.info(
                                "d015_primary | ge={} instructions={} turnover_est={}",
                                dec.gross_exposure_target,
                                len(plan.instructions),
                                plan.estimated_turnover,
                            )
                            for instr in plan.instructions:
                                px = await _resolve_price_for_symbol(instr.symbol)
                                ac = _asset_class_lookup(instr.symbol, batch_candidates)
                                routed = self.router.route(ac, instr.symbol)
                                if routed is None:
                                    continue
                                rs = risk_signal_from_execution_instruction(
                                    instr,
                                    signal_id=str(uuid.uuid4()),
                                    broker=routed,
                                    asset_class=ac,
                                    price=px,
                                )
                                ok = await _process_signal(rs, symbol_hint=instr.symbol)
                                if ok:
                                    executed += 1

                    now_ts = datetime.now(timezone.utc).timestamp()
                    if now_ts >= next_reconcile_at:
                        await self.execution_engine.reconcile_positions(session_factory=session_factory)
                        next_reconcile_at = now_ts + self.reconcile_interval_sec

                    await publish_runner_heartbeat(
                        bus, runner_name="orchestrator",
                        symbols=symbols, generated=generated, executed=executed,
                        extra={"paper_mode": self.paper_mode},
                    )
                    self.last_iteration_at = datetime.now(timezone.utc)
                    self.iterations += 1
                    self.last_error = None
                    logger.info("trading_loop | iteration #{} | generated={} executed={}", self.iterations, generated, executed)

                except Exception as exc:
                    self.last_error = str(exc)[:300]
                    logger.exception("trading_loop | iteration failed: {}", exc)

                    try:
                        await dispose_engine(engine)
                    except Exception:
                        pass
                    engine, session_factory = await init_async_database()
                    if session_factory is None:
                        logger.error("trading_loop | DB reconnect failed — will retry next iteration")
                    else:
                        bus = CommandBus(session_factory)

                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=self.loop_interval_sec)
                    break
                except asyncio.TimeoutError:
                    pass

        except asyncio.CancelledError:
            logger.info("trading_loop | cancelled")
        except Exception as exc:
            self.last_error = str(exc)[:300]
            logger.exception("trading_loop | fatal: {}", exc)
        finally:
            self._running = False
            if engine is not None:
                try:
                    await dispose_engine(engine)
                except Exception:
                    pass

    def status_dict(self) -> dict[str, Any]:
        return {
            "running": self.is_running,
            "iterations": self.iterations,
            "last_iteration_at": self.last_iteration_at.isoformat() if self.last_iteration_at else None,
            "last_error": self.last_error,
            "paper_mode": self.paper_mode,
            "capital_pct": self.capital_pct,
        }
