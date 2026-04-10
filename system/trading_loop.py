"""
system/trading_loop.py
======================
Wraps the M5 trading loop as a controllable async task that the orchestrator
can start and stop on demand.  Reuses all existing strategy/risk/execution logic.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml
from loguru import logger

from ai.news_classifier import NewsClassifier
from ai.pipeline import AIPipeline
from ai.regime import filter_by_allowed_strategies
from control.command_bus import CommandBus
from control.runner_control import (
    apply_control_commands,
    hydrate_risk_parameters_from_bus,
    publish_runner_heartbeat,
)
from control.runtime import set_risk_engine
from execution.engine import ExecutionEngine
from execution.router import SmartOrderRouter
from risk.engine import RiskEngine, RiskVerdict
from risk.m8_loader import merge_m8_into_risk_cfg
from run_m3 import (
    _load_portfolio_state,
    _load_recent_features,
    _persist_position_snapshot,
    _persist_signal,
    _pick_best_signal,
    _upsert_daily_pnl,
)
from signals.engine import SignalEngine
from storage.db import dispose_engine, init_async_database
from storage.models import AIOutputLog
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
        for name, adapter in self._broker_manager.adapters.items():
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
            ai_cfg = _load_yaml("config/ai.yaml")
            discovery_cfg = _load_yaml("config/discovery.yaml")

            symbols_raw = pipeline_cfg.get("symbols", [])
            symbols = [s.strip() for s in symbols_raw if s.strip()] if isinstance(symbols_raw, list) else []
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
            set_risk_engine(self.risk_engine)

            ai_enabled = bool(ai_cfg.get("enabled", True))
            ai_classifier = NewsClassifier() if ai_enabled else None
            ai_pipeline = AIPipeline(ai_cfg.get("pipeline", {}), classifier=ai_classifier) if ai_enabled else None

            strat_cfg = strategies_cfg.get("strategies", {})
            momentum = MomentumBreakoutStrategy(strat_cfg.get("momentum_breakout", {}))
            mean_rev = MeanReversionStrategy(strat_cfg.get("mean_reversion", {}))
            strategies = {momentum.name: momentum, mean_rev.name: mean_rev}

            bus = CommandBus(session_factory)
            for name, strategy in strategies.items():
                state_v = await bus.get_state(f"strategy.enabled.{name}", None)
                if state_v is not None:
                    strategy.enabled = bool(state_v)
            await hydrate_risk_parameters_from_bus(bus, self.risk_engine)

            next_reconcile_at = datetime.now(timezone.utc).timestamp()

            while not self._stop_event.is_set():
                self._check_late_brokers()
                effective_value = self.portfolio_value * self.capital_pct
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
                            ai_result = await ai_pipeline.compute(session_factory, symbols)
                            await ai_pipeline.persist(session_factory, ai_result)
                        except Exception as exc:
                            logger.warning("trading_loop | AI pipeline error (continuing): {}", exc)

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

                        routed = self.router.route(signal.asset_class, signal.symbol)
                        if routed is None:
                            continue
                        signal.broker = routed

                        portfolio_state = await _load_portfolio_state(
                            session_factory,
                            fallback_portfolio_value=Decimal(str(effective_value)),
                            signal_price_fallback=signal.suggested_price,
                        )
                        self.risk_engine.update_high_watermark(
                            Decimal(str(portfolio_state.get("high_watermark_value", effective_value)))
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
                            continue

                        await _persist_signal(
                            session_factory, signal,
                            paper_mode=self.paper_mode,
                            timeframe=self.timeframe,
                            feature_ts=datetime.now(timezone.utc),
                        )
                        generated += 1

                        result = await self.execution_engine.execute(
                            signal, risk_decision, session_factory=session_factory,
                        )
                        if result is not None:
                            executed += 1
                            await _persist_position_snapshot(session_factory, portfolio_state)
                            await _upsert_daily_pnl(session_factory, portfolio_state)

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
