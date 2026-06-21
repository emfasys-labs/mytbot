"""
Controllable async trading loop (orchestrator). Implementation: :class:`TradingLoop` in this package.
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from collections import Counter
from copy import deepcopy
from dataclasses import is_dataclass, replace as dataclass_replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

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
from control.runtime import set_execution_engine, set_risk_engine
from execution.d015_instruction_executor import risk_signal_from_execution_instruction
from execution.engine import ExecutionEngine
from execution.planner import build_execution_plan
from execution.router import BROKER_ASSET_MAP, SmartOrderRouter
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
from system.fusion_shadow import log_fusion_shadow_for_signal
from system.dashboard_publish import (
    publish_dashboard_snapshot_d015,
    publish_dashboard_snapshot_global_edge,
    publish_dashboard_snapshot_heartbeat,
)
from system.funnel_telemetry import (
    get_default_funnel_telemetry,
    record_strategy_candidate_rows,
)
from system.portfolio_equity import live_portfolio_value
from risk.m8_loader import merge_m8_into_risk_cfg
from risk.options_env import merge_options_env_into_risk_cfg
from core.broker_paper import NO_NATIVE_PAPER_POSITION_BROKERS
from core.pnl import unrealised_pnl_account_currency
from run_m3 import (
    _apply_signal_to_portfolio_state,
    _load_portfolio_state,
    _load_recent_features,
    _persist_position_snapshot,
    _persist_signal,
    _pick_best_signal,
    _refresh_position_marks_and_persist,
    _upsert_daily_pnl,
)
from signals.accumulator import SignalAccumulator
from signals.engine import RawSignal, SignalEngine
from storage.db import dispose_engine, get_app_database, init_async_database
from storage.discovery import persist_anomaly_log, persist_thesis_log
from storage.models import AIOutputLog, FeatureSnapshot, OrderLog
from strategies.mean_reversion import MeanReversionStrategy
from strategies.momentum import MomentumBreakoutStrategy
from strategies.volume_flow import VolumeFlowStrategy
from strategies.event_driven import EventDrivenNewsStrategy
from strategies.pairs_trading import PairsTradingStrategy
from strategies.volatility_regime import VolatilityRegimeStrategy
from strategies.regime_rotation import RegimeRotationStrategy
from strategies.trend_breakout import TrendBreakoutStrategy
from strategies.trend_following import TrendFollowingStrategy
from system.demand_engine import DemandEngine
from signals.meta_labeler import filter_candidates as meta_filter_candidates, keep_raw_signal as meta_keep_raw_signal
from signals.meta_adaptation import compute_dynamic_strategy_bias

from data.arb_observability import log_arb_event
from data.capability_registry import CapabilityRegistry
from data.funding_rates import FundingRateDataProvider
from execution.execution_planner import ExecutionPlanner
from execution.latency_predictor import LatencyPredictor
from execution.orderbook_analyzer import OrderBookAnalyzer
from execution.venue_selector import VenueSelector
from portfolio.global_edge_coordinator import (
    GlobalEdgeCoordinator,
    cross_exchange_dict_to_strategy_opportunity,
    dedupe_opportunities_by_symbol,
    funding_arb_signal_to_strategy_opportunity,
    held_positions_from_portfolio,
    signal_candidate_to_strategy_opportunity,
)
from system.strategy_candidate_log import persist_rows as persist_strategy_candidate_rows
from system.strategy_candidate_log import row as strategy_candidate_row
from portfolio.treasury_manager import TreasuryManager, merge_treasury_into_portfolio_state
from portfolio.portfolio_orchestrator import (
    BookPosition,
    OrchestratorConfig,
    build_intents_from_candidates,
    orchestrate,
)
from backtest.edge_gate import EdgeGateRegistry, EdgeGateThresholds
from signals.arb_bridge import CoordinatorAction, process_coordinator_action
from signals.microstructure.liquidity_tracker import LiquidityTracker
from strategies.arbitrage.cross_exchange import CrossExchangeArbitrageStrategy
from strategies.arbitrage.funding_rate import FundingRateArbitrageStrategy

from system.trading_loop.candidate_collection import (
    apply_regime_filter_with_logs,
    apply_regime_weighting,
    collect_raw_signals_for_symbol,
)
from system.trading_loop.helpers import (
    apply_saved_mode_to_risk_cfg,
    asset_class_for_symbol,
    attach_forecast_sequence_history,
    broker_symbol_for,
    d015_legacy_fallback,
    enrich_candidate_liquidity,
    enrich_candidate_volume_z,
    enrich_signal_liquidity,
    enrich_signal_volume_z,
    is_crypto_symbol,
    is_futures_symbol,
    load_yaml,
)
from ai.news_scores import news_score_for_symbol as _news_score_for_symbol


_DIRECTIONAL_SIDES = {"long", "short", "buy", "sell"}
_SIDE_TO_ORDER_SIDE = {"long": "buy", "short": "sell", "buy": "buy", "sell": "sell"}


def _decimal_state_value(state: dict[str, Any], key: str, fallback: Decimal) -> Decimal:
    try:
        value = Decimal(str(state.get(key, fallback)))
    except Exception:  # noqa: BLE001
        return fallback
    return value if value >= 0 else fallback


_SEQ_FC_ENABLED: bool | None = None


def _forecast_sequence_member_enabled() -> bool:
    """True iff a Phase-B sequence forecast member is configured AND enabled.

    Memoised (config is process-static; a change needs a restart anyway).
    Default state is False (no sequence member / bridge disabled), so the
    loop's sequence-history attachment is genuinely zero-overhead until a
    governed model is registered + activated.
    """
    global _SEQ_FC_ENABLED
    if _SEQ_FC_ENABLED is None:
        try:
            from signals.forecast_bridge import ForecastBridgeConfig

            cfg = ForecastBridgeConfig.load()
            _SEQ_FC_ENABLED = bool(
                cfg.enabled and any(m.kind == "sequence" for m in cfg.members)
            )
        except Exception:  # noqa: BLE001
            _SEQ_FC_ENABLED = False
    return _SEQ_FC_ENABLED


async def _load_working_order_keys(session_factory: Any) -> set[tuple[str, str]]:
    """Return {(SYMBOL, order_side)} for every order still working at a broker.

    Used to short-circuit the global edge coordinator so it doesn't spend its
    per-tick action budget re-proposing opportunities whose prior limit orders
    are still sitting on the book — the execution engine would dedup them
    anyway, producing ``executed=0`` for an otherwise-healthy iteration.
    """
    if session_factory is None:
        return set()
    try:
        async with session_factory() as session:
            stmt = select(OrderLog.symbol, OrderLog.side).where(
                OrderLog.status.in_(("pending", "open", "partially_filled"))
            )
            rows = (await session.execute(stmt)).all()
    except Exception as exc:  # noqa: BLE001
        logger.warning("trading_loop | working-order key lookup failed | {}", exc)
        return set()
    out: set[tuple[str, str]] = set()
    for sym, side in rows:
        s = (sym or "").strip().upper()
        sd = (side or "").strip().lower()
        if s and sd:
            out.add((s, sd))
    return out


async def _merge_live_broker_positions_into_portfolio_state(
    portfolio_state: dict[str, Any],
    broker_manager: Any,
    *,
    paper_mode: bool = True,
) -> None:
    """Overlay broker-authoritative positions onto a DB-derived portfolio state."""
    if paper_mode:
        # Paper orders are filled into the local PositionLog ledger. Broker
        # adapters may expose an empty native paper book (or be temporarily
        # offline), and overlaying that here erases positions before the
        # zero-allocation flatten path can close them.
        return
    adapters = getattr(broker_manager, "adapters", None)
    if not isinstance(adapters, dict):
        return
    positions = dict(portfolio_state.get("positions") or {})
    for broker_name, adapter in list(adapters.items()):
        bname = str(broker_name or "").strip().lower()
        if not bname:
            continue
        if paper_mode and bname in NO_NATIVE_PAPER_POSITION_BROKERS:
            # Kraken/Binance/Bybit do not have exchange-native paper position
            # books in this project. In paper mode the DB snapshot from
            # simulated fills is the book; overlaying live adapter positions
            # reopens exposure that the paper engine has already closed.
            continue
        try:
            live_positions = await adapter.get_positions()
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "trading_loop | live position overlay skipped | broker={} | {}",
                bname,
                exc,
            )
            continue
        positions = {
            sym: row
            for sym, row in positions.items()
            if str((row or {}).get("broker", "")).strip().lower() != bname
        }
        for p in live_positions or []:
            symbol = str(getattr(p, "symbol", "") or "").strip().upper()
            if not symbol:
                continue
            try:
                qty = Decimal(str(getattr(p, "quantity", "0") or "0"))
                px = Decimal(str(getattr(p, "current_price", "0") or "0"))
                avg = Decimal(str(getattr(p, "avg_entry_price", px) or px))
            except Exception:  # noqa: BLE001
                continue
            if qty == 0 or px <= 0:
                continue
            asset_raw = getattr(p, "asset_class", "equity")
            asset = str(getattr(asset_raw, "value", asset_raw) or "equity").strip().lower()
            entry: dict[str, Any] = {
                "quantity": qty,
                "avg_entry_price": avg,
                "current_price": px,
                "asset_class": asset,
                "broker": bname,
            }
            im = getattr(p, "instrument_metadata", None)
            if isinstance(im, dict):
                entry["instrument_metadata"] = im
            positions[symbol] = entry

    gross = Decimal("0")
    symbol_exposure: dict[str, Decimal] = {}
    asset_class_exposure: dict[str, Decimal] = {}
    for sym, row in positions.items():
        try:
            qty = Decimal(str(row.get("quantity", "0") or "0"))
            px = Decimal(str(row.get("current_price", "0") or "0"))
        except Exception:  # noqa: BLE001
            continue
        notional = abs(qty) * px
        if notional <= 0:
            continue
        symbol_exposure[str(sym)] = symbol_exposure.get(str(sym), Decimal("0")) + notional
        asset = str(row.get("asset_class", "") or "").strip().lower()
        if asset:
            asset_class_exposure[asset] = asset_class_exposure.get(asset, Decimal("0")) + notional
        gross += notional

    portfolio_state["positions"] = positions
    portfolio_state["current_gross_exposure"] = gross
    portfolio_state["symbol_exposure"] = symbol_exposure
    portfolio_state["asset_class_exposure"] = asset_class_exposure


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
        pipeline_wake_event: asyncio.Event | None = None,
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
        self._pipeline_wake_event = pipeline_wake_event

        self._task: asyncio.Task | None = None
        self._control_poll_task: asyncio.Task | None = None
        self._broker_join_task: asyncio.Task | None = None
        self._control_bus: CommandBus | None = None
        self._strategies: dict[str, Any] = {}
        self._stop_event = asyncio.Event()
        self._wake_event = asyncio.Event()
        self._running = False
        # Slider-event signal — when set by ``request_iteration("capital_allocation_changed")``,
        # the next iteration body cancels working orders + forces a fresh
        # cancel/grow/trim plan against the new target gross.
        self._capital_change_pending: bool = False
        self._last_capital_pct_seen: float | None = None
        # Cash-target memory for the shed-to-target branch.
        self._last_adaptive_cash_target: Decimal | None = None
        self._last_adaptive_held_cash_used: Decimal | None = None

        self.risk_engine: RiskEngine | None = None
        self.execution_engine: ExecutionEngine | None = None
        self.sig_engine: SignalEngine | None = None
        self.router: SmartOrderRouter | None = None
        self.last_iteration_at: datetime | None = None
        self.iterations: int = 0
        self.last_error: str | None = None
        # ── Boot warmup churn-guard ──────────────────────────────────────
        # After ANY process (re)start — supervisor crash-recovery, a machine
        # wake, a deploy — the very first allocation cycle(s) run with
        # freshly-rebuilt in-memory + possibly-degraded persisted state. If
        # the allocator is allowed to cull/recycle/replace immediately it can
        # close positions and re-open them moments later (the documented
        # close→reopen bleed, ~$1k/restart). For a short warmup window after
        # start we therefore suppress *position-reducing* coordinator actions
        # only. Opens, holds, arbitrage and — critically — every risk /
        # stop-loss / profit-harvest exit (separate code paths) are NEVER
        # gated. Self-clearing by wall-clock + one completed iteration.
        # Rolling per-bucket / per-symbol net-of-cost attribution, refreshed
        # from the DB every EDGE_ATTRIB_REFRESH_EVERY_N iterations and injected
        # into the coordinator cfg so a persistently bleeding action-class /
        # symbol faces a steeply widened (auto-recovering) edge bar.
        self._edge_attrib_cache: dict[str, Any] | None = None
        # Last-good live price per symbol for the mark-to-market sweep:
        # {sym: (Decimal, monotonic_ts)}. Lets the persisted mark stay real
        # for forex/futures/crypto symbols that have no M2 feature bars,
        # which otherwise carried their entry price forever.
        self._mark_px_cache: dict[tuple[str, str] | str, tuple[Decimal, float]] = {}
        self._loop_started_monotonic: float | None = None
        try:
            self._warmup_min_sec: float = max(
                0.0, float(os.getenv("MYTBOT_WARMUP_MIN_SEC", "120"))
            )
        except (TypeError, ValueError):
            self._warmup_min_sec = 120.0
        self._warmup_suppress_logged = False
        self._warmup_cleared_logged = False
        # Rolling signal-density input for the adaptive_mode classifier.
        # Updated at the end of every iteration; read at the start of the
        # next. None on fresh boot so the classifier falls back to defaults.
        self._last_generated_count: int = 0
        # Audit #6: telemetry for swallowed hot-path exceptions. The loop has
        # many broad ``except Exception`` guards that intentionally degrade
        # gracefully rather than crash a trading iteration — but historically
        # they vanished into a debug log, so a metadata/feedback/persistence
        # subsystem could be 100% failing with zero operator-visible signal.
        # ``_swallow`` records the failure by label and the per-iteration
        # total is logged with the iteration summary.
        self._swallow_counts: dict[str, int] = {}
        self._swallow_iter_total: int = 0

        self._global_edge_cfg: dict[str, Any] = {}
        self._enable_arbitrage: bool = False
        self._use_global_edge: bool = False
        self._treasury: Any = None
        self._latency_predictor: LatencyPredictor | None = None
        self._arb_stack: dict[str, Any] | None = None

    def _get_held_cash_used(self, portfolio_dict: dict[str, Any]) -> Decimal:
        """Calculate the actual capital/cash deployed in the portfolio."""
        from portfolio.global_edge_coordinator import (
            held_positions_from_portfolio,
            cash_factor_for_asset_class as _cf,
        )

        held = held_positions_from_portfolio(portfolio_dict)
        _ge_cf_overrides = None
        if hasattr(self, "_global_edge_cfg") and isinstance(self._global_edge_cfg, dict):
            _ge_cf_overrides = self._global_edge_cfg.get("cash_factors") or None

        held_cash_used = sum(
            (
                h.notional
                * _cf(
                    str((h.metadata or {}).get("asset_class") or ""),
                    _ge_cf_overrides,
                    symbol=h.symbol,
                )
                for h in held
            ),
            Decimal("0"),
        )
        return held_cash_used

    async def _check_and_trigger_unallocated_capital_discovery(
        self,
        *,
        portfolio_dict: dict[str, Any],
        total_equity: Decimal,
        executed_count: int,
    ) -> None:
        """Check if we have substantial unallocated capital target and trigger pipeline discovery."""
        if self.capital_pct <= 0.0:
            return

        if self._pipeline_wake_event is None:
            return

        held_cash_used = self._get_held_cash_used(portfolio_dict)
        
        # Calculate target cash deployment
        gross_fraction = Decimal("1.0")
        if hasattr(self, "_global_edge_cfg") and isinstance(self._global_edge_cfg, dict):
            _adaptive_cfg = self._global_edge_cfg.get("adaptive") or {}
            current_mode = self._read_active_mode()
            _legacy_mode_block = _adaptive_cfg.get("mode")
            if isinstance(_legacy_mode_block, dict):
                _mode_raw_cfg = (
                    _legacy_mode_block.get(current_mode)
                    or _legacy_mode_block.get("trader")
                    or {}
                )
                _gf_src = _mode_raw_cfg.get("gross_fraction", _adaptive_cfg.get("gross_fraction", "1.0"))
            else:
                _gf_src = _adaptive_cfg.get("gross_fraction", "1.0")
            try:
                gross_fraction = Decimal(str(_gf_src))
            except Exception:
                gross_fraction = Decimal("1.0")

        target_capital = total_equity * Decimal(str(self.capital_pct)) * gross_fraction
        remaining_cash = target_capital - held_cash_used

        # Dynamic threshold based on default position percentage
        default_pos_pct = Decimal("0.05")
        if self.sig_engine is not None and hasattr(self.sig_engine, "config"):
            try:
                default_pos_pct = Decimal(str(self.sig_engine.config.get("default_position_pct", "0.05")))
            except Exception:
                pass

        # Smart dynamic threshold (we need enough cash to open at least one position)
        threshold = total_equity * default_pos_pct
        
        # Safe bounds: threshold must be between 1% and 10% of NAV
        threshold = max(total_equity * Decimal("0.01"), min(threshold, total_equity * Decimal("0.10")))

        if remaining_cash > threshold:
            # Dynamic cooldown equal to current loop cadence
            cooldown_sec = float(self.loop_interval_sec)
            now = time.monotonic()
            last_trigger = getattr(self, "_last_unallocated_discovery_trigger_at", 0.0)

            if now - last_trigger >= cooldown_sec:
                logger.info(
                    "trading_loop | unallocated capital detected: held_cash={} target={} remaining={} (threshold={}) | "
                    "waking pipeline runner to force discovery",
                    held_cash_used,
                    target_capital,
                    remaining_cash,
                    threshold,
                )
                self._last_unallocated_discovery_trigger_at = now
                self._pipeline_wake_event.set()
            else:
                logger.debug(
                    "trading_loop | unallocated capital exists but discovery is in cooldown ({:.1f}s remaining)",
                    cooldown_sec - (now - last_trigger),
                )

    @property
    def is_running(self) -> bool:
        return self._running and self._task is not None and not self._task.done()

    def _swallow(self, where: str, exc: BaseException) -> None:
        """Record a swallowed hot-path exception (audit #6).

        Use in place of a bare ``except Exception: pass`` / ``...: debug(...)``
        in the candidate→execution path. The iteration keeps degrading
        gracefully (no crash) but the failure is now counted by ``where`` and
        the per-iteration total is surfaced in the iteration summary log, so
        a silently-failing subsystem is no longer invisible.
        """
        try:
            self._swallow_counts[where] = self._swallow_counts.get(where, 0) + 1
            self._swallow_iter_total += 1
            logger.debug("trading_loop | swallowed[{}] | {}", where, exc)
        except Exception:  # noqa: BLE001 — telemetry must never raise
            pass

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
                if self.execution_engine is not None:
                    self.execution_engine.add_allowed_broker(name)
                logger.info("trading_loop | late broker joined: {}", name)

    def request_iteration(self, reason: str = "operator_request") -> None:
        """Wake the loop so operator control changes take effect promptly."""
        self._wake_event.set()
        # Slider-driven wakes (capital_allocation_changed) require an
        # additional cancel-and-replan beyond a normal wake; flag it so the
        # iteration body knows to honour the new target gross immediately.
        if reason == "capital_allocation_changed":
            self._capital_change_pending = True
        logger.info("trading_loop | wake requested | {}", reason)

    async def _wait_for_next_iteration(self, timeout_sec: float) -> bool:
        """Wait for stop, timeout, or an operator wake.

        Returns True when the loop should stop; False means continue with the
        next trading iteration.
        """
        if self._stop_event.is_set():
            return True
        stop_task = asyncio.create_task(self._stop_event.wait())
        wake_task = asyncio.create_task(self._wake_event.wait())
        try:
            done, pending = await asyncio.wait(
                {stop_task, wake_task},
                timeout=max(0.0, float(timeout_sec)),
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            if stop_task in done and stop_task.result():
                return True
            if wake_task in done and wake_task.result():
                logger.info("trading_loop | wake consumed — running immediate iteration")
                self._wake_event.clear()
                return False
            return False
        finally:
            for task in (stop_task, wake_task):
                if not task.done():
                    task.cancel()

    def _start_broker_join_poll(self) -> None:
        """Poll broker_manager adapters so late connections are usable quickly."""
        if self._broker_manager is None:
            return
        if self._broker_join_task is not None and not self._broker_join_task.done():
            return
        self._broker_join_task = asyncio.create_task(
            self._broker_join_poll(), name="broker-join-poll"
        )

    async def _broker_join_poll(self) -> None:
        try:
            poll_sec = max(1.0, float(os.getenv("BROKER_JOIN_POLL_SEC", "2")))
        except ValueError:
            poll_sec = 2.0
        while not self._stop_event.is_set():
            try:
                self._check_late_brokers()
            except Exception as exc:  # noqa: BLE001
                logger.debug("trading_loop | broker join poll failed: {}", exc)
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=poll_sec)
                return
            except asyncio.TimeoutError:
                pass

    async def start(self) -> None:
        if self.is_running:
            logger.warning("trading_loop | already running")
            return
        self._stop_event.clear()
        self._loop_started_monotonic = time.monotonic()
        self._warmup_suppress_logged = False
        self._warmup_cleared_logged = False
        self._task = asyncio.create_task(self._run(), name="trading-loop")
        logger.info(
            "trading_loop | started (boot warmup churn-guard: {:.0f}s)",
            self._warmup_min_sec,
        )

    def _in_boot_warmup(self) -> bool:
        """True during the post-(re)start window when culling must be held.

        Clears once BOTH a minimum wall-clock has elapsed AND at least one
        full iteration has completed — i.e. state has been rebuilt and a
        cycle has run cleanly. A disabled window (``MYTBOT_WARMUP_MIN_SEC=0``)
        still requires one completed iteration so the first cycle after a
        crash never culls on un-rebuilt state.
        """
        if self._loop_started_monotonic is None:
            return False
        if self.iterations < 1:
            return True
        elapsed = time.monotonic() - self._loop_started_monotonic
        return elapsed < self._warmup_min_sec

    @staticmethod
    def _action_is_position_reducing(action: Any) -> bool:
        """Cull / recycle / shed / trim / close / flatten — the bleed surface.

        Opens (``open_strategy``) and arbitrage are NOT reducing and pass
        through. Risk / stop-loss / profit-harvest exits never reach this
        coordinator-action path, so they are inherently unaffected.
        """
        kind = str(getattr(action, "kind", "") or "").strip().lower()
        if kind in {"trim_symbol", "close_symbol", "flatten_symbol"}:
            return True
        if kind.startswith(("close", "flatten", "exit", "trim", "reduce")):
            return True
        meta = getattr(action, "metadata", None) or {}
        sizing_path = str(meta.get("sizing_path", "") or "").strip().lower()
        if sizing_path in {"capital_recycle", "adaptive_shed_to_target"}:
            return True
        if meta.get("capital_recycle_reason"):
            return True
        strat = str(getattr(action, "strategy_name", "") or "").strip().lower()
        return strat in {"capital_recycle", "adaptive_shed"}

    def _suppress_reducing_actions_during_warmup(self, actions: list) -> list:
        """Drop position-reducing actions while in the boot warmup window.

        This is the bulletproofing against the restart close→reopen bleed:
        no matter WHY the process restarted (supervisor recovery, machine
        wake, deploy), it cannot cull-then-rebuy during the fragile first
        cycle. Holding a position one extra cycle is free; churning it is
        not. Returns the (possibly filtered) action list.
        """
        if not actions or not self._in_boot_warmup():
            return actions
        kept = [a for a in actions if not self._action_is_position_reducing(a)]
        dropped = len(actions) - len(kept)
        if dropped and not self._warmup_suppress_logged:
            self._warmup_suppress_logged = True
            logger.warning(
                "trading_loop | BOOT WARMUP — suppressing {} position-reducing "
                "action(s) this cycle (anti-churn after restart); opens, "
                "arbitrage and all risk/stop-loss exits are unaffected",
                dropped,
            )
        return kept

    async def _live_price_oracle(
        self,
        sym: str,
        *,
        broker: str = "",
        avg_entry_price: Decimal | None = None,
    ) -> Decimal:
        """Venue-native last price for the mark-to-market sweep.

        Prefers the position broker's adapter so unrelated CFD/crypto venues
        cannot pollute futures marks. A last-good cache bridges transient misses.
        """
        from core.instruments import futures_multiplier, normalize_futures_mark_price

        bm = self._broker_manager
        s = str(sym or "").strip()
        b = str(broker or "").strip().lower()
        ref = avg_entry_price if avg_entry_price is not None and avg_entry_price > 0 else None
        if not s or bm is None:
            return Decimal("0")
        adapters = getattr(bm, "adapters", None)
        if not isinstance(adapters, dict) or not adapters:
            return Decimal("0")

        async def _probe(name: str, adapter) -> Decimal:
            try:
                raw = await asyncio.wait_for(adapter.get_last_price(s), timeout=1.0)
            except Exception:  # noqa: BLE001
                return Decimal("0")
            try:
                px = Decimal(str(raw))
            except Exception:  # noqa: BLE001
                return Decimal("0")
            if px <= 0:
                return Decimal("0")
            if futures_multiplier(s) is not None:
                return normalize_futures_mark_price(s, px, avg_entry_price=ref)
            return px

        ordered = list(adapters.items())
        if b and b in adapters:
            ordered = [(b, adapters[b])] + [(n, a) for n, a in adapters.items() if n != b]
        if futures_multiplier(s) is not None and b and b in adapters:
            ordered = [(b, adapters[b])]

        for name, adapter in ordered:
            px = await _probe(name, adapter)
            if px > 0:
                cache_key = (b or name, s.upper())
                self._mark_px_cache[cache_key] = (px, time.monotonic())
                return px
        try:
            ttl = max(0.0, float(os.getenv("MARK_PX_CACHE_TTL_SEC", "180")))
        except (TypeError, ValueError):
            ttl = 180.0
        cache_key = (b, s.upper()) if b else None
        cached = self._mark_px_cache.get(cache_key) if cache_key else None
        if cached is None and not b:
            cached = self._mark_px_cache.get(s.upper())
        if cached is not None and ttl > 0 and (time.monotonic() - cached[1]) <= ttl:
            return cached[0]
        return Decimal("0")

    async def stop(self) -> None:
        if not self.is_running:
            return
        logger.info("trading_loop | stopping...")
        self._stop_event.set()
        if self._control_poll_task is not None:
            self._control_poll_task.cancel()
            try:
                await self._control_poll_task
            except (asyncio.CancelledError, Exception):
                pass
            self._control_poll_task = None
        if self._broker_join_task is not None:
            self._broker_join_task.cancel()
            try:
                await self._broker_join_task
            except (asyncio.CancelledError, Exception):
                pass
            self._broker_join_task = None
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        self._running = False
        logger.info("trading_loop | stopped")

    async def _control_command_poll(self) -> None:
        """Process DB control commands on a short interval so kill/params are not blocked by long loop iterations."""
        try:
            poll_sec = max(0.5, float(os.getenv("CONTROL_COMMAND_POLL_SEC", "2")))
        except ValueError:
            poll_sec = 2.0
        while not self._stop_event.is_set():
            bus = self._control_bus
            strat = self._strategies
            if bus is not None and self.risk_engine is not None:
                try:
                    await apply_control_commands(
                        bus,
                        risk_engine=self.risk_engine,
                        execution_engine=self.execution_engine,
                        strategies=strat or {},
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("trading_loop | control poll failed: {}", exc)
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=poll_sec)
                return
            except asyncio.TimeoutError:
                pass

    async def _run(self) -> None:
        self._running = True
        engine = None
        owns_engine = False
        try:
            strategies_cfg = load_yaml("config/strategies.yaml")
            pipeline_cfg = load_yaml("config/data_pipeline.yaml")
            risk_cfg = load_yaml("config/risk_limits.yaml")
            merge_m8_into_risk_cfg(risk_cfg, "config/m8_micro_live.yaml")
            merge_options_env_into_risk_cfg(risk_cfg)
            legacy_fb = d015_legacy_fallback()
            if legacy_fb:
                risk_cfg["allocator_d015_primary"] = False
                risk_cfg["allocator_d015_enabled"] = (
                    os.getenv("ALLOCATOR_D015_ENABLED", "").strip().lower() in ("1", "true", "yes", "on")
                )
            else:
                risk_cfg["allocator_d015_enabled"] = True
                risk_cfg["allocator_d015_primary"] = True
            ai_cfg = load_yaml("config/ai.yaml")
            discovery_cfg = load_yaml("config/discovery.yaml")

            symbols_raw = pipeline_cfg.get("symbols", [])
            symbols = [s.strip() for s in symbols_raw if s.strip()] if isinstance(symbols_raw, list) else []
            # Keep base symbols permanently in the loop even in dynamic mode so
            # we always monitor a stable liquid anchor set (SPY/QQQ/BTC/ETH, etc).
            base_symbols = list(dict.fromkeys(symbols))
            if not symbols:
                logger.warning("trading_loop | no symbols in config/data_pipeline.yaml — idle")
                self._running = False
                return

            bound_engine, bound_sf = get_app_database()
            if bound_sf is not None:
                engine = bound_engine
                session_factory = bound_sf
                owns_engine = False
                logger.info("trading_loop | using shared app database pool (no second engine)")
            else:
                engine, session_factory = await init_async_database()
                owns_engine = engine is not None
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
                broker_manager=self._broker_manager,
            )
            self._start_broker_join_poll()
            _se_cfg = strategies_cfg.get("signal_engine", {}) or {}
            _acc = (
                SignalAccumulator()
                if bool(_se_cfg.get("use_signal_accumulator", True))
                else None
            )
            self.sig_engine = SignalEngine(_se_cfg, accumulator=_acc)
            self.risk_engine = RiskEngine(risk_cfg)
            if self.risk_engine.is_killed:
                logger.critical("trading_loop | risk kill switch is latched from persisted state; new orders remain blocked")
            # Apply persisted mode overrides (if user selected a mode before this start)
            apply_saved_mode_to_risk_cfg(self.risk_engine)
            set_risk_engine(self.risk_engine)
            ge_yaml = load_yaml("config/global_edge.yaml")
            self._global_edge_cfg = ge_yaml if isinstance(ge_yaml, dict) else {}
            self._enable_arbitrage = os.getenv("ENABLE_ARBITRAGE", "").strip().lower() in ("1", "true", "yes")
            self._use_global_edge = (
                os.getenv("GLOBAL_EDGE_COORDINATOR", "").strip().lower() in ("1", "true", "yes")
                or bool(self._global_edge_cfg.get("enabled"))
            )
            self._treasury = TreasuryManager(logger=logger)
            self._latency_predictor = LatencyPredictor()
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
            volume_flow = VolumeFlowStrategy(strat_cfg.get("volume_flow", {}))
            event_driven = EventDrivenNewsStrategy(strat_cfg.get("event_driven_news", {}))
            pairs_trading = PairsTradingStrategy(strat_cfg.get("pairs_trading", {}))
            volatility_regime = VolatilityRegimeStrategy(strat_cfg.get("volatility_regime", {}))
            regime_rotation = RegimeRotationStrategy(strat_cfg.get("regime_rotation", {}))
            trend_breakout = TrendBreakoutStrategy(strat_cfg.get("trend_breakout", {}))      # D158
            trend_following = TrendFollowingStrategy(strat_cfg.get("trend_following", {}))    # D158
            demand_engine = DemandEngine(strat_cfg.get("demand_engine", {}))
            meta_cfg = dict(strat_cfg.get("meta_labeling", {}) or {})
            meta_enabled = bool(meta_cfg.get("enabled", False))
            meta_dynamic_bias: dict[str, float] = {}
            meta_adapt_every_n = max(1, int(meta_cfg.get("adapt_every_n_iterations", 12) or 12))
            meta_adapt_lookback_h = max(4, int(meta_cfg.get("adapt_lookback_hours", 72) or 72))
            meta_adapt_min_samples = max(5, int(meta_cfg.get("adapt_min_samples", 16) or 16))
            demand_alert_threshold = float(strat_cfg.get("demand_engine", {}).get("alert_threshold", 0.55) or 0.55)
            demand_mode_thresholds = dict(strat_cfg.get("demand_engine", {}).get("mode_alert_thresholds", {}) or {})
            last_demand_alert: dict[str, Any] | None = None
            demand_alert_history: list[dict[str, Any]] = []
            meta_dynamic_diag: dict[str, Any] = {}
            routing_decay_every_n = max(1, int(strat_cfg.get("routing_learning", {}).get("decay_every_n_iterations", 8) or 8))
            routing_decay_rate = float(strat_cfg.get("routing_learning", {}).get("decay_rate", 0.02) or 0.02)
            routing_decay_adaptive = bool(strat_cfg.get("routing_learning", {}).get("adaptive_decay", True))
            routing_persist_every_n = max(1, int(strat_cfg.get("routing_learning", {}).get("persist_every_n_iterations", 5) or 5))
            strategies = {
                momentum.name: momentum,
                mean_rev.name: mean_rev,
                volume_flow.name: volume_flow,
                event_driven.name: event_driven,
                pairs_trading.name: pairs_trading,
                volatility_regime.name: volatility_regime,
                regime_rotation.name: regime_rotation,
                trend_breakout.name: trend_breakout,      # D158 sniper
                trend_following.name: trend_following,     # D158 shotgun
            }

            bus = CommandBus(session_factory)
            alloc_cfg = load_allocation()
            profile_modes_cfg = load_profile_modes()
            # Mode-aware iteration cadence. YAML key is ``loop_cadence_sec``
            # under ``config/profile_modes.yaml``. Missing/invalid entries fall
            # back to ``self.loop_interval_sec`` so operators can disable this
            # feature simply by deleting the YAML block.
            mode_cadence_map = self._load_mode_cadence_map()
            for name, strategy in strategies.items():
                state_v = await bus.get_state(f"strategy.enabled.{name}", None)
                if state_v is not None:
                    strategy.enabled = bool(state_v)
            await hydrate_risk_parameters_from_bus(bus, self.risk_engine)

            self._control_bus = bus
            self._strategies = strategies
            self._control_poll_task = asyncio.create_task(self._control_command_poll(), name="control-command-poll")
            try:
                rq = await bus.get_state("routing.quality.state", None)
                if isinstance(rq, dict) and self.router is not None:
                    self.router.import_quality_state(rq)
            except Exception:  # noqa: BLE001
                pass

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

            # Futures execution is gated until a futures-contract resolver
            # ships for IBKR (``ES=F`` → ``Future("ES", "202506", "CME")``).
            # Until then we still generate + rank + log futures signals for
            # visibility, but short-circuit order placement so we don't send
            # nonsense Stock contracts to the broker.
            futures_execution_enabled = os.getenv(
                "FUTURES_EXECUTION_ENABLED", "0"
            ).strip().lower() in ("1", "true", "yes", "on")
            funnel = get_default_funnel_telemetry()

            async def _process_signal(
                signal,
                *,
                symbol_hint: str | None = None,
                sc_log_buffer: list[dict[str, Any]] | None = None,
            ) -> bool:
                routed = self.router.route(
                    signal.asset_class,
                    signal.symbol,
                    metadata=getattr(signal, "metadata", None),
                )
                strategy_key = str(getattr(signal, "strategy", "") or "unknown").strip() or "unknown"
                if routed is None:
                    funnel.record_execution_blocked(strategy_key)
                    return False
                signal.broker = routed
                # Futures data-only gate. Signal still logs; order never lands.
                if is_futures_symbol(signal.symbol) and not futures_execution_enabled:
                    if not isinstance(getattr(signal, "metadata", None), dict):
                        signal.metadata = {}
                    signal.metadata["execution_gated"] = "futures_disabled"
                    logger.info(
                        "FUTURES DATA-ONLY | skipping execution for {} (set FUTURES_EXECUTION_ENABLED=1 once the contract resolver ships)",
                        signal.symbol,
                    )
                    await _persist_signal(
                        session_factory, signal,
                        paper_mode=self.paper_mode,
                        timeframe=self.timeframe,
                        feature_ts=datetime.now(timezone.utc),
                    )
                    funnel.record_execution_blocked(strategy_key)
                    return False
                # Rewrite the pipeline ticker to the broker's native form
                # before the order builder sees it (e.g. ``EURUSD=X`` → ``EURUSD``).
                native = broker_symbol_for(signal.symbol, routed)
                if native and native != signal.symbol:
                    if not isinstance(getattr(signal, "metadata", None), dict):
                        signal.metadata = {}
                    signal.metadata.setdefault("pipeline_symbol", signal.symbol)
                    signal.symbol = native
                # AI-fusion spine (Phase A/B) shadow — SHARED chokepoint for
                # BOTH the legacy and D015/global-edge batch paths, so the
                # shadow audit actually fires in production. OWN env gate
                # (FUSION_SHADOW, default off), read-only, exception-safe;
                # reads already-computed metadata (forecast/meta/accumulator/
                # regime/etc.) and changes no live decision.
                _fs_md = signal.metadata if isinstance(getattr(signal, "metadata", None), dict) else {}
                await log_fusion_shadow_for_signal(
                    symbol=signal.symbol,
                    side=str(getattr(signal, "side", "") or ""),
                    confidence=float(getattr(signal, "confidence", 0.0) or 0.0),
                    metadata=dict(_fs_md),
                    mode=str(_fs_md.get("profile_mode") or "") or None,
                )
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
                    funnel.record_risk_rejected(strategy_key)
                    if sc_log_buffer is not None:
                        _md0 = (
                            "d015"
                            if (
                                isinstance(getattr(signal, "metadata", None), dict)
                                and (signal.metadata or {}).get("d015_executor")
                            )
                            else "legacy"
                        )
                        sc_log_buffer.append(
                            strategy_candidate_row(
                                symbol=str(signal.symbol),
                                strategy=str(getattr(signal, "strategy", "") or ""),
                                side=str(getattr(signal, "side", "") or ""),
                                confidence=float(getattr(signal, "confidence", 0) or 0),
                                status="risk_rejected",
                                reason=str(risk_decision.reason or risk_decision.verdict.value),
                                loop_iteration=self.iterations,
                                metadata={
                                    "path": _md0,
                                    "verdict": str(risk_decision.verdict.value),
                                    "checks_failed": list(risk_decision.checks_failed or [])[:32],
                                },
                            )
                        )
                    await _persist_signal(
                        session_factory, signal,
                        paper_mode=self.paper_mode,
                        timeframe=self.timeframe,
                        feature_ts=datetime.now(timezone.utc),
                    )
                    return False
                funnel.record_risk_approved(strategy_key)
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
                    funnel.record_execution_blocked(strategy_key)
                    engine_reason = str(
                        getattr(self.execution_engine, "last_skip_reason", "") or "execution_no_result"
                    )
                    if sc_log_buffer is not None:
                        sc_log_buffer.append(
                            strategy_candidate_row(
                                symbol=str(signal.symbol),
                                strategy=str(getattr(signal, "strategy", "") or ""),
                                side=str(getattr(signal, "side", "") or ""),
                                confidence=float(getattr(signal, "confidence", 0) or 0),
                                status="execution_incomplete",
                                reason=engine_reason,
                                loop_iteration=self.iterations,
                                metadata={
                                    "execution_stage": "no_order_from_engine",
                                    "execution_skip_reason": engine_reason,
                                },
                            )
                        )
                    try:
                        turnover_hint = float(signal.metadata.get("target_notional", 0.0)) if isinstance(signal.metadata, dict) else 0.0
                        liq_hint = float(signal.metadata.get("volume_z_score", 0.0)) if isinstance(signal.metadata, dict) else 0.0
                        self.router.record_execution_feedback(
                            broker=str(signal.broker or ""),
                            symbol=str(signal.symbol or ""),
                            filled=False,
                            slippage_bps=None,
                            turnover_hint=turnover_hint,
                            liquidity_hint=liq_hint,
                        )
                    except Exception:  # noqa: BLE001
                        pass
                    return False
                status_val = str(getattr(getattr(result, "status", None), "value", getattr(result, "status", ""))).lower()
                if status_val != "filled":
                    funnel.record_execution_blocked(strategy_key)
                    if sc_log_buffer is not None:
                        sc_log_buffer.append(
                            strategy_candidate_row(
                                symbol=str(signal.symbol),
                                strategy=str(getattr(signal, "strategy", "") or ""),
                                side=str(getattr(signal, "side", "") or ""),
                                confidence=float(getattr(signal, "confidence", 0) or 0),
                                status="execution_incomplete",
                                reason=f"order_status_{status_val}",
                                loop_iteration=self.iterations,
                                metadata={"execution_stage": "non_filled", "order_status": status_val},
                            )
                        )
                    try:
                        turnover_hint = float(signal.metadata.get("target_notional", 0.0)) if isinstance(signal.metadata, dict) else 0.0
                        liq_hint = float(signal.metadata.get("volume_z_score", 0.0)) if isinstance(signal.metadata, dict) else 0.0
                        self.router.record_execution_feedback(
                            broker=str(signal.broker or ""),
                            symbol=str(signal.symbol or ""),
                            filled=False,
                            slippage_bps=None,
                            turnover_hint=turnover_hint,
                            liquidity_hint=liq_hint,
                        )
                    except Exception:  # noqa: BLE001
                        pass
                    # Only treat fully filled orders as executed positions.
                    return False
                filled_qty = Decimal(str(getattr(result, "filled_quantity", "0") or "0"))
                if filled_qty <= 0:
                    funnel.record_execution_blocked(strategy_key)
                    if sc_log_buffer is not None:
                        sc_log_buffer.append(
                            strategy_candidate_row(
                                symbol=str(signal.symbol),
                                strategy=str(getattr(signal, "strategy", "") or ""),
                                side=str(getattr(signal, "side", "") or ""),
                                confidence=float(getattr(signal, "confidence", 0) or 0),
                                status="execution_incomplete",
                                reason="execution_zero_fill",
                                loop_iteration=self.iterations,
                                metadata={"execution_stage": "zero_filled_qty", "order_status": status_val},
                            )
                        )
                    try:
                        turnover_hint = float(signal.metadata.get("target_notional", 0.0)) if isinstance(signal.metadata, dict) else 0.0
                        liq_hint = float(signal.metadata.get("volume_z_score", 0.0)) if isinstance(signal.metadata, dict) else 0.0
                        self.router.record_execution_feedback(
                            broker=str(signal.broker or ""),
                            symbol=str(signal.symbol or ""),
                            filled=False,
                            slippage_bps=None,
                            turnover_hint=turnover_hint,
                            liquidity_hint=liq_hint,
                        )
                    except Exception:  # noqa: BLE001
                        pass
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
                # Realised PnL accumulation. Without this, _upsert_daily_pnl
                # writes back whatever it just read (circular load), so the
                # daily_pnl row stays at 0 forever even after closing trades.
                # Only the *closing* portion of a fill realises PnL — adding
                # to a position is just rebasing the average entry.
                try:
                    from run_m5 import _estimate_realized_pnl_from_fill as _est_realised
                    # Estimate against the pre-trade book. The freshly loaded
                    # post-trade state may already have removed/rebased the
                    # position, which makes closes look like zero realised P&L.
                    realised_delta = _est_realised(portfolio_state, signal, result)
                except Exception as exc:  # noqa: BLE001
                    logger.debug("trading_loop | realised pnl est failed: {}", exc)
                    realised_delta = Decimal("0")
                fee_dec = Decimal("0")
                fee_raw = getattr(result, "fee", None)
                if fee_raw is not None:
                    try:
                        fee_dec = Decimal(str(fee_raw))
                    except Exception:  # noqa: BLE001
                        fee_dec = Decimal("0")
                # Fees apply to every filled execution, not only realised closes.
                # Persist this delta unconditionally so daily_pnl.total_fees
                # includes opening/add flows as well as closes.
                post_trade_state["fees_today_delta"] = fee_dec
                if realised_delta and realised_delta != Decimal("0"):
                    try:
                        prev = Decimal(str(post_trade_state.get("daily_realized_pnl", "0")))
                    except Exception:  # noqa: BLE001
                        prev = Decimal("0")
                    post_trade_state["daily_realized_pnl"] = prev + realised_delta
                    if realised_delta < 0:
                        try:
                            self.risk_engine.record_loss(abs(realised_delta))
                        except Exception:  # noqa: BLE001
                            pass
                    elif realised_delta > 0:
                        try:
                            self.risk_engine.record_win()
                        except Exception:  # noqa: BLE001
                            pass
                    post_trade_state.update(self.risk_engine.snapshot_runtime_state())

                _apply_signal_to_portfolio_state(post_trade_state, signal, result)
                await _persist_position_snapshot(session_factory, post_trade_state)
                await _upsert_daily_pnl(session_factory, post_trade_state)
                # D115 — anti-churn post-fill cooldown. Stamp every confirmed
                # fill (open, add, reduce-only, close) so duplicate strategy
                # signals on the same (broker, symbol) are blocked for the
                # configured window. Safe no-op when the gate is disabled.
                try:
                    is_reduce_only = bool(
                        isinstance(signal.metadata, dict)
                        and (
                            signal.metadata.get("reduce_only")
                            or signal.metadata.get("close_only")
                            or signal.metadata.get("flatten_all")
                            or str(signal.metadata.get("coordinator_kind", "")).lower() == "trim_symbol"
                        )
                    )
                    self.sig_engine.record_fill(
                        broker=str(signal.broker or ""),
                        symbol=str(signal.symbol or ""),
                        side=str(signal.side or ""),
                        is_reduce_only=is_reduce_only,
                    )
                except Exception:  # noqa: BLE001
                    pass
                try:
                    slip_bps = None
                    avg_fill = getattr(result, "avg_fill_price", None)
                    if avg_fill is not None and signal.suggested_price is not None and signal.suggested_price > 0:
                        slip_bps = float((Decimal(str(avg_fill)) - Decimal(str(signal.suggested_price))) / Decimal(str(signal.suggested_price)) * Decimal("10000"))
                    self.router.record_execution_feedback(
                        broker=str(signal.broker or ""),
                        symbol=str(signal.symbol or ""),
                        filled=True,
                        slippage_bps=slip_bps,
                        turnover_hint=float(signal.metadata.get("target_notional", 0.0)) if isinstance(signal.metadata, dict) else 0.0,
                        liquidity_hint=float(signal.metadata.get("volume_z_score", 0.0)) if isinstance(signal.metadata, dict) else 0.0,
                    )
                except Exception:  # noqa: BLE001
                    pass
                if sc_log_buffer is not None:
                    _md1 = (
                        "d015"
                        if (
                            isinstance(getattr(signal, "metadata", None), dict)
                            and (signal.metadata or {}).get("d015_executor")
                        )
                        else "legacy"
                    )
                    sc_log_buffer.append(
                        strategy_candidate_row(
                            symbol=str(signal.symbol),
                            strategy=str(getattr(signal, "strategy", "") or ""),
                            side=str(getattr(signal, "side", "") or ""),
                            confidence=float(getattr(signal, "confidence", 0) or 0),
                            status="executed",
                            reason="order_filled",
                            loop_iteration=self.iterations,
                            metadata={"path": _md1},
                        )
                    )
                funnel.record_execution_approved(strategy_key)
                funnel.record_executed(strategy_key)
                return True

            while not self._stop_event.is_set():
                if self.iterations == 0:
                    logger.info("trading_loop | startup flush — first iteration running immediately")
                self._check_late_brokers()
                # ── Slider-event handler ────────────────────────────────
                # When the operator moves the capital slider, immediately
                # cancel any working orders sized to the prior tradable so
                # the upcoming iteration's adaptive coordinator can resize
                # against the new target. Trim of oversized positions on
                # slider-down happens inside the iteration body via the
                # propose_flatten/propose_actions paths driven by the new
                # ``tradable`` value.
                if self._capital_change_pending:
                    self._capital_change_pending = False
                    try:
                        if self.execution_engine is not None:
                            cancelled = await self.execution_engine.cancel_working_orders(
                                session_factory=session_factory,
                                reason="capital_allocation_changed",
                            )
                            if cancelled:
                                logger.info(
                                    "trading_loop | slider event cancelled {} working order(s)",
                                    cancelled,
                                )
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("trading_loop | slider-event cancel failed | {}", exc)
                    self._last_capital_pct_seen = self.capital_pct
                elif self._last_capital_pct_seen is None:
                    self._last_capital_pct_seen = self.capital_pct
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
                try:
                    total_equity = await asyncio.wait_for(
                        live_portfolio_value(self._broker_manager),
                        timeout=float(os.getenv("BROKER_NAV_TIMEOUT_SEC", "20")),
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("trading_loop | live NAV timeout/error | {}", exc)
                    total_equity = Decimal(str(self.portfolio_value))
                if total_equity <= 0:
                    total_equity = Decimal(str(self.portfolio_value))
                tradable = total_equity * Decimal(str(self.capital_pct))
                effective_value = float(tradable)

                portfolio_state = await _load_portfolio_state(
                    session_factory,
                    fallback_portfolio_value=total_equity,
                    signal_price_fallback=None,
                    capital_pct=Decimal(str(self.capital_pct)),
                )
                total_equity = portfolio_state["portfolio_value"]
                tradable = portfolio_state["tradable_capital"]
                effective_value = float(tradable)

                # D122 — dynamic threshold context. Computed once per
                # loop iteration and stamped on each raw signal so
                # downstream gates (meta-labeler, Wave 9) can resolve
                # thresholds against live regime + operator-deployment
                # intent without any static numbers in YAML.
                _pmeta = portfolio_state.get("metadata") if isinstance(portfolio_state, dict) else None
                _pmeta = _pmeta if isinstance(_pmeta, dict) else {}
                try:
                    _ctx_mss = float(_pmeta.get("market_state_score", 1.0))
                except (TypeError, ValueError):
                    _ctx_mss = 1.0
                try:
                    _ctx_vol = float(_pmeta.get("market_volatility_scalar", 1.0))
                except (TypeError, ValueError):
                    _ctx_vol = 1.0
                try:
                    _ctx_nav = float(total_equity)
                    _ctx_deployed = -1.0
                    if bus is not None:
                        try:
                            _snap_for_pressure = await bus.get_state("dashboard.snapshot", None)
                            if isinstance(_snap_for_pressure, dict):
                                _snap_port = _snap_for_pressure.get("portfolio")
                                if isinstance(_snap_port, dict):
                                    _ctx_deployed = float(_snap_port.get("cash_deployed_pct", -1.0))
                        except Exception:  # noqa: BLE001
                            _ctx_deployed = -1.0
                    if _ctx_deployed < 0:
                        _ctx_cash_used = float(self._get_held_cash_used(portfolio_state))
                        _ctx_deployed = (_ctx_cash_used / _ctx_nav) if _ctx_nav > 0 else 0.0
                except (TypeError, ValueError, ZeroDivisionError):
                    try:
                        _ctx_gross = float(portfolio_state.get("current_gross_exposure", 0) or 0)
                        _ctx_nav = float(total_equity)
                        _ctx_deployed = (_ctx_gross / _ctx_nav) if _ctx_nav > 0 else 0.0
                    except (TypeError, ValueError, ZeroDivisionError):
                        _ctx_deployed = 0.0
                _ctx_deploy_pressure = max(0.0, min(1.0, float(self.capital_pct) - _ctx_deployed))
                dynamic_threshold_ctx: dict[str, Any] = {
                    "market_state_score": _ctx_mss,
                    "market_volatility_scalar": _ctx_vol,
                    "deployment_pressure": _ctx_deploy_pressure,
                }
                # D141 — config-version hash. Computed once per iteration
                # (it only changes when YAML changes). Stamped on every
                # raw signal's metadata via ``dynamic_threshold_ctx``
                # propagation, so fills carry it into the ledger for
                # P&L attribution per threshold-regime.
                try:
                    from system.dynamic_thresholds import config_version as _cv
                    _ctx_config_hash = _cv()
                    if _ctx_config_hash:
                        dynamic_threshold_ctx["config_hash"] = _ctx_config_hash
                except Exception:  # noqa: BLE001
                    pass
                if meta_enabled and (self.iterations % meta_adapt_every_n == 0):
                    try:
                        dyn_bias, dyn_diag = await compute_dynamic_strategy_bias(
                            session_factory,
                            lookback_hours=meta_adapt_lookback_h,
                            min_samples=meta_adapt_min_samples,
                        )
                        if dyn_bias:
                            meta_dynamic_bias = dyn_bias
                            await bus.set_state("meta_label.dynamic_strategy_bias", dyn_bias)
                            await bus.set_state("meta_label.dynamic_diagnostics", dyn_diag)
                            meta_dynamic_diag = dict(dyn_diag or {})
                    except Exception as exc:  # noqa: BLE001
                        logger.debug("meta_adaptation failed: {}", exc)
                if self.router is not None and (self.iterations % routing_decay_every_n == 0):
                    try:
                        self.router.apply_decay(routing_decay_rate, adaptive=routing_decay_adaptive)
                    except Exception:  # noqa: BLE001
                        pass
                try:
                    generated = 0
                    executed = 0
                    dashboard_snapshot_published = False
                    symbols_with_features = 0
                    symbols_feature_empty = 0

                    now_ts = datetime.now(timezone.utc).timestamp()
                    if now_ts >= next_reconcile_at:
                        await self.execution_engine.reconcile_positions(session_factory=session_factory)
                        next_reconcile_at = now_ts + self.reconcile_interval_sec

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

                    if (
                        self.sig_engine.accumulator is not None
                        and ai_result is not None
                    ):
                        self.sig_engine.accumulator.feed_ai_pipeline_result(
                            ai_result,
                            symbols,
                            now=datetime.now(timezone.utc),
                        )

                    # ── Adaptive mode (Phase 0) ─────────────────────────────
                    # Mode is now derived from market state every iteration —
                    # operator can't set it. The classifier biases toward
                    # ``hunter`` and only steps down on objective adverse
                    # evidence (drawdown breach, emergency news, vol spike
                    # when those inputs are wired in Phase 4). The result is
                    # written to ``active_mode.json`` so every consumer that
                    # already reads that file picks up the new value without
                    # any further wiring.
                    try:
                        from system.adaptive_mode import (
                            ModeInputs,
                            classify_market_mode,
                            serialise_for_active_mode_json,
                        )
                        # Drawdown: today's NAV vs the persisted HWM. Negative
                        # values are losses.
                        nav_dd_pct = None
                        try:
                            hwm = Decimal(str(portfolio_state.get("high_watermark_value", 0) or 0))
                            pv = Decimal(str(portfolio_state.get("portfolio_value", 0) or 0))
                            if hwm > 0 and pv > 0:
                                nav_dd_pct = float((pv - hwm) / hwm)
                        except Exception:  # noqa: BLE001
                            nav_dd_pct = None
                        # Signal density: candidates generated in the prior
                        # iteration. Fresh boot → None (defaults pass through).
                        sig_density = float(
                            getattr(self, "_last_generated_count", 0) or 0
                        )
                        # Emergency news flag — Phase 0 leaves this False until
                        # Phase 4 wires in the AI pipeline anomaly bus. Defender
                        # via news today only triggers if someone sets it
                        # externally; that's intentional.
                        decision = classify_market_mode(
                            ModeInputs(
                                nav_drawdown_pct=nav_dd_pct,
                                recent_signal_density=sig_density,
                                emergency_news_active=False,
                            )
                        )
                        mode_raw = decision.mode
                        # Persist the decision payload so the dashboard can
                        # render "why" the mode is what it is.
                        import json as _json
                        from pathlib import Path as _Path
                        _runtime_dir = _Path("data/runtime")
                        _runtime_dir.mkdir(parents=True, exist_ok=True)
                        _mf = _runtime_dir / "active_mode.json"
                        _mf.write_text(
                            _json.dumps(serialise_for_active_mode_json(decision), default=str),
                            encoding="utf-8",
                        )
                        if self.iterations % 5 == 0 or decision.mode != "hunter":
                            logger.info(
                                "adaptive_mode | mode={} reason={} dd_pct={} sig_density={}",
                                decision.mode,
                                decision.reason,
                                f"{nav_dd_pct:.4f}" if nav_dd_pct is not None else "n/a",
                                f"{sig_density:.1f}" if sig_density is not None else "n/a",
                            )
                    except Exception as exc:  # noqa: BLE001
                        # Fallback to whatever active_mode.json contains; if
                        # that fails too, default to ``trader`` to keep the
                        # rest of the loop safe.
                        logger.debug("adaptive_mode | classifier failed, falling back: {}", exc)
                        mode_raw = "trader"
                        try:
                            import json as _json
                            from pathlib import Path as _Path
                            _mf = _Path("data/runtime/active_mode.json")
                            if _mf.is_file():
                                mode_raw = str(
                                    _json.loads(_mf.read_text(encoding="utf-8")).get("mode", "trader")
                                )
                        except Exception:  # noqa: BLE001
                            pass
                    # D140 + D141 — pull the live regime snapshot (label,
                    # continuous feature components, smoothed market_state_score)
                    # so it can be (a) injected into each strategy's config
                    # for ``dynamic_thresholds`` formulas to consume, and
                    # (b) passed into ``apply_regime_weighting`` below for
                    # the per-symbol confidence weighting. Both come from
                    # the dashboard snapshot written by the previous
                    # iteration's ``compute_regime_state_async``. Missing
                    # snapshot → empty inputs → passthrough.
                    live_regime_label = ""
                    live_regime_features: dict[str, Any] = {}
                    _live_mss: Any = 0
                    try:
                        if bus is not None:
                            snap = await bus.get_state("dashboard.snapshot", None)
                            if isinstance(snap, dict):
                                regime_block = snap.get("regime")
                                if isinstance(regime_block, dict):
                                    live_regime_label = str(
                                        regime_block.get("regime_label") or ""
                                    ).strip().lower()
                                    components = regime_block.get("components")
                                    if isinstance(components, dict):
                                        for k, v in components.items():
                                            try:
                                                live_regime_features[str(k)] = float(v)
                                            except (TypeError, ValueError):
                                                continue
                            _val = await bus.get_state("smoothed_market_state_score", None)
                            if _val is not None:
                                _live_mss = _val
                    except Exception:  # noqa: BLE001 — never break the loop on a stale snapshot
                        live_regime_label = ""
                        live_regime_features = {}
                        _live_mss = 0
                    regime_min_conf = float(
                        risk_cfg.get("min_signal_confidence", 0.50) or 0.50
                    )

                    # D141 Phase 3 — pull each strategy's recent net P&L
                    # so dynamic sizing can shrink bleeding strategies and
                    # grow winners. Cached 5 min so we don't hit the DB
                    # every iteration; falls back to {} on any failure.
                    _strategy_pnl: dict[str, dict[str, Any]] = {}
                    try:
                        from system.strategy_pnl_health import fetch_strategy_pnl_recent

                        _strategy_pnl = await fetch_strategy_pnl_recent(session_factory)
                    except Exception as exc:  # noqa: BLE001
                        logger.debug("trading_loop | strategy pnl fetch failed: {}", exc)
                        _strategy_pnl = {}

                    # D141 — hash of the live dynamic_thresholds + regime_weights
                    # YAML blocks. Stamped on every signal's metadata so a fill
                    # in the ledger can be attributed to the exact threshold
                    # regime that produced it. Empty string when YAML cannot be
                    # read (degraded but non-fatal).
                    try:
                        from system.dynamic_thresholds import config_version
                        _config_hash = config_version()
                    except Exception:  # noqa: BLE001
                        _config_hash = ""

                    try:
                        pmeta = portfolio_state.get("metadata")
                        if not isinstance(pmeta, dict):
                            pmeta = {}
                            portfolio_state["metadata"] = pmeta
                        dyn_meta = pmeta.get("dynamic_thresholds")
                        if not isinstance(dyn_meta, dict):
                            dyn_meta = {}
                            pmeta["dynamic_thresholds"] = dyn_meta
                        per_strategy_meta: dict[str, dict[str, Any]] = {}
                        for _name, _stats in (_strategy_pnl or {}).items():
                            if not isinstance(_stats, dict):
                                continue
                            per_strategy_meta[str(_name)] = {
                                "recent_net_pnl": _stats.get("net_pnl", 0),
                                "recent_fills": _stats.get("fills", 0),
                                "recent_win_rate": _stats.get("win_rate", 0),
                                "sample_target_notional": _stats.get("sample_target_notional", 0),
                            }
                        if per_strategy_meta:
                            dyn_meta["per_strategy"] = per_strategy_meta
                    except Exception as exc:  # noqa: BLE001
                        logger.debug("trading_loop | risk strategy-performance metadata failed: {}", exc)

                    for name, strategy in strategies.items():
                        try:
                            if isinstance(getattr(strategy, "config", None), dict):
                                strategy.config["_active_profile_mode"] = mode_raw
                                strategy.config["_market_state_score"] = _live_mss
                                strategy.config["_regime_label"] = live_regime_label
                                strategy.config["_regime_features"] = (
                                    dict(live_regime_features) if live_regime_features else {}
                                )
                                # Per-strategy live P&L health.
                                _stats = _strategy_pnl.get(name, {})
                                strategy.config["_strategy_pnl_recent"] = str(
                                    _stats.get("net_pnl", "0")
                                )
                                strategy.config["_strategy_fills_recent"] = int(
                                    _stats.get("fills", 0) or 0
                                )
                                try:
                                    from system.strategy_quarantine import decide_strategy_quarantine

                                    _q = decide_strategy_quarantine(
                                        name,
                                        _stats,
                                        strat_cfg.get("strategy_quarantine") if isinstance(strat_cfg, dict) else None,
                                    )
                                    strategy.config["_strategy_quarantine_mult"] = str(_q.multiplier)
                                    strategy.config["_strategy_quarantine_state"] = _q.state
                                    strategy.config["_strategy_quarantine_reason"] = _q.reason
                                except Exception as exc:  # noqa: BLE001
                                    logger.debug("trading_loop | strategy quarantine failed: {}", exc)
                                    strategy.config["_strategy_quarantine_mult"] = "1"
                                    strategy.config["_strategy_quarantine_state"] = "unknown"
                                # ``total_equity`` is the loop's live NAV
                                # computed above via ``live_portfolio_value``.
                                strategy.config["_nav"] = str(total_equity or 0)
                                strategy.config["_config_hash"] = _config_hash
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
                            return Decimal("0")
                        col = "close" if "close" in df_p.columns else None
                        if col is None:
                            return Decimal("0")
                        try:
                            return Decimal(str(float(df_p[col].iloc[-1])))
                        except Exception:  # noqa: BLE001
                            return Decimal("0")

                    def _asset_class_lookup(sym: str, cands: list) -> str:
                        for c in cands:
                            if getattr(c, "symbol", None) == sym:
                                return str(getattr(c, "asset_class", "equity") or "equity")
                        return "equity"

                    use_legacy = legacy_fb
                    batch_candidates: list[Any] = []
                    demand_score = 0.0
                    demand_trend = "flat"
                    demand_confidence = 0.0
                    demand_components: dict[str, Any] = {}

                    if use_legacy:
                        demand_ctx = demand_engine.compute(
                            ai_result=ai_result,
                            feature_map={},
                        )
                        demand_score = float(demand_ctx.score)
                        demand_trend = str(demand_ctx.trend)
                        demand_confidence = float(demand_ctx.confidence)
                        demand_components = dict(demand_ctx.components or {})
                        threshold_eff = demand_alert_threshold
                        try:
                            if mode_raw in demand_mode_thresholds:
                                threshold_eff = float(demand_mode_thresholds.get(mode_raw))
                        except (TypeError, ValueError):
                            threshold_eff = demand_alert_threshold
                        if (
                            abs(demand_score) >= threshold_eff
                            and (last_demand_alert is None or last_demand_alert.get("trend") != demand_trend)
                        ):
                            last_demand_alert = {
                                "at": datetime.now(timezone.utc).isoformat(),
                                "trend": demand_trend,
                                "score": round(float(demand_score), 6),
                                "confidence": round(float(demand_confidence), 6),
                                "kind": "demand_regime_shift",
                                "threshold": round(float(threshold_eff), 6),
                                "mode": mode_raw,
                            }
                            demand_alert_history.append(dict(last_demand_alert))
                            demand_alert_history = demand_alert_history[-20:]
                        sc_log_rows_legacy: list[dict[str, Any]] = []
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

                            sym_ac = asset_class_for_symbol(symbol)
                            raw_candidates, sym_sc = collect_raw_signals_for_symbol(
                                symbol=symbol,
                                df=df,
                                sym_ac=sym_ac,
                                momentum=momentum,
                                mean_rev=mean_rev,
                                volume_flow=volume_flow,
                                volatility_regime=volatility_regime,
                                event_driven=event_driven,
                                regime_rotation=regime_rotation,
                                trend_breakout=trend_breakout,
                                trend_following=trend_following,
                                ai_result=ai_result,
                                demand_score=demand_score,
                                demand_trend=demand_trend,
                                demand_confidence=demand_confidence,
                                loop_iteration=self.iterations,
                            )
                            sc_log_rows_legacy.extend(sym_sc)

                            raw_candidates = apply_regime_filter_with_logs(
                                raw_candidates,
                                symbol=symbol,
                                ai_result=ai_result,
                                ai_pipeline=ai_pipeline,
                                sc_rows=sc_log_rows_legacy,
                                loop_iteration=self.iterations,
                            )

                            # D140 — regime-aware confidence weighting,
                            # computed live from market-feature components.
                            raw_candidates = apply_regime_weighting(
                                raw_candidates,
                                symbol=symbol,
                                regime_label=live_regime_label,
                                market_features=live_regime_features or None,
                                min_confidence=regime_min_conf,
                                deployment_pressure=_ctx_deploy_pressure,
                                sc_rows=sc_log_rows_legacy,
                                loop_iteration=self.iterations,
                            )

                            # D167.1 — drop edge-gate-BLOCKED sides BEFORE the
                            # anti-churn gate records them (see batch path).
                            raw_candidates = self._filter_edge_gate_blocked_raws(
                                raw_candidates,
                                strategies_cfg,
                                symbol=symbol,
                                sc_log_buffer=sc_log_rows_legacy,
                            )

                            pre_meta_r = list(raw_candidates)
                            if meta_enabled and raw_candidates:
                                meta_cfg_eff = dict(meta_cfg)
                                static_bias = dict(meta_cfg_eff.get("strategy_bias", {}) or {})
                                for k, v in meta_dynamic_bias.items():
                                    try:
                                        static_bias[k] = float(static_bias.get(k, 0.0)) + float(v)
                                    except (TypeError, ValueError):
                                        static_bias[k] = float(v)
                                meta_cfg_eff["strategy_bias"] = static_bias
                                raw_candidates = [
                                    r
                                    for r in raw_candidates
                                    if meta_keep_raw_signal(
                                        r,
                                        demand_score=demand_score,
                                        cfg=meta_cfg_eff,
                                        mode=mode_raw,
                                    )
                                ]
                                kept_meta_raw = {(r.symbol, r.strategy) for r in raw_candidates}
                                for r in pre_meta_r:
                                    if (r.symbol, r.strategy) in kept_meta_raw:
                                        continue
                                    sc_log_rows_legacy.append(
                                        strategy_candidate_row(
                                            symbol=symbol,
                                            strategy=str(r.strategy),
                                            side=str(r.side) if r.side else None,
                                            confidence=r.confidence,
                                            status="filtered_meta",
                                            reason="meta_label_filter_raw",
                                            loop_iteration=self.iterations,
                                        )
                                    )

                            raw = _pick_best_signal(raw_candidates)
                            if raw is None:
                                continue
                            for r in raw_candidates:
                                if (r.symbol, r.strategy) == (raw.symbol, raw.strategy):
                                    continue
                                sc_log_rows_legacy.append(
                                    strategy_candidate_row(
                                        symbol=symbol,
                                        strategy=str(r.strategy),
                                        side=str(r.side) if r.side else None,
                                        confidence=r.confidence,
                                        status="lost_to_strategy",
                                        reason="lower_confidence_vs_peer",
                                        winner_strategy=str(raw.strategy),
                                        loop_iteration=self.iterations,
                                    )
                                )

                            # D122 — stamp dynamic threshold context on the
                            # legacy single-signal path as well.
                            _rm = dict(getattr(raw, "metadata", {}) or {})
                            if dynamic_threshold_ctx:
                                for _k, _v in dynamic_threshold_ctx.items():
                                    _rm.setdefault(_k, _v)
                                raw.metadata = _rm
                            signal = self.sig_engine.process(
                                raw,
                                portfolio_value=Decimal(str(effective_value)),
                                news_score=(ai_result.news_scores.get(symbol) if ai_result else None),
                            )
                            if signal is None:
                                continue

                            # Relabel the signal with the true asset class for
                            # this ticker so SmartOrderRouter picks the right
                            # venue (equity → ibkr/alpaca, crypto → binance/
                            # kraken, forex → ibkr). Strategies no longer need
                            # to hard-code the class themselves.
                            resolved_ac = asset_class_for_symbol(symbol)
                            if resolved_ac and signal.asset_class != resolved_ac:
                                signal.asset_class = resolved_ac

                            if ai_result is not None:
                                signal.metadata["ai_macro_regime"] = ai_result.macro_regime
                                signal.metadata["ai_macro_confidence"] = ai_result.macro_confidence

                            enrich_signal_volume_z(signal, df)
                            enrich_signal_liquidity(signal, df)

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

                        if sc_log_rows_legacy:
                            record_strategy_candidate_rows(sc_log_rows_legacy)
                            await persist_strategy_candidate_rows(session_factory, sc_log_rows_legacy)

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
                                    ds.metadata = dict(getattr(ds, "metadata", {}) or {})
                                    if dynamic_threshold_ctx:
                                        for _k, _v in dynamic_threshold_ctx.items():
                                            ds.metadata.setdefault(_k, _v)
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
                        batch_candidates.clear()
                        sc_log_rows: list[dict[str, Any]] = []
                        feature_map: dict[str, Any] = {}
                        demand_ctx = demand_engine.compute(
                            ai_result=ai_result,
                            feature_map={},
                        )
                        demand_score = float(demand_ctx.score)
                        demand_trend = str(demand_ctx.trend)
                        demand_confidence = float(demand_ctx.confidence)
                        demand_components = dict(demand_ctx.components or {})
                        threshold_eff = demand_alert_threshold
                        try:
                            if mode_raw in demand_mode_thresholds:
                                threshold_eff = float(demand_mode_thresholds.get(mode_raw))
                        except (TypeError, ValueError):
                            threshold_eff = demand_alert_threshold
                        if (
                            abs(demand_score) >= threshold_eff
                            and (last_demand_alert is None or last_demand_alert.get("trend") != demand_trend)
                        ):
                            last_demand_alert = {
                                "at": datetime.now(timezone.utc).isoformat(),
                                "trend": demand_trend,
                                "score": round(float(demand_score), 6),
                                "confidence": round(float(demand_confidence), 6),
                                "kind": "demand_regime_shift",
                                "threshold": round(float(threshold_eff), 6),
                                "mode": mode_raw,
                            }
                            demand_alert_history.append(dict(last_demand_alert))
                            demand_alert_history = demand_alert_history[-20:]
                        # Audit #7: prefetch every symbol's feature window
                        # CONCURRENTLY (bounded) instead of one blocking DB
                        # round-trip per symbol inside the loop. Previously a
                        # single slow/locked feature read stalled every
                        # downstream symbol's opportunity for the whole
                        # iteration. The per-symbol processing below stays
                        # sequential (it mutates shared buffers and ordering
                        # matters) — only the I/O is parallelized.
                        try:
                            _pf = int(os.getenv("FEATURE_PREFETCH_CONCURRENCY", "8") or 8)
                        except (TypeError, ValueError):
                            _pf = 8
                        _pf = max(1, min(_pf, 32))
                        _feat_sem = asyncio.Semaphore(_pf)

                        async def _prefetch_one(_sym: str):
                            async with _feat_sem:
                                try:
                                    return _sym, await _load_recent_features(
                                        session_factory,
                                        symbol=_sym,
                                        timeframe=self.timeframe,
                                        lookback_bars=self.lookback_bars,
                                    )
                                except Exception as exc:  # noqa: BLE001
                                    self._swallow("feature_prefetch", exc)
                                    return _sym, None

                        _prefetched = await asyncio.gather(
                            *[_prefetch_one(s) for s in symbols]
                        )
                        _feature_by_symbol: dict[str, Any] = {
                            s: res for s, res in _prefetched
                        }

                        for symbol in symbols:
                            if self._stop_event.is_set():
                                break

                            _res = _feature_by_symbol.get(symbol)
                            if _res is None:
                                symbols_feature_empty += 1
                                continue
                            df, feature_ts = _res
                            if df.empty:
                                symbols_feature_empty += 1
                                continue
                            symbols_with_features += 1
                            feature_map[symbol.strip().upper()] = df

                            sym_ac = asset_class_for_symbol(symbol)
                            raw_candidates, sym_sc = collect_raw_signals_for_symbol(
                                symbol=symbol,
                                df=df,
                                sym_ac=sym_ac,
                                momentum=momentum,
                                mean_rev=mean_rev,
                                volume_flow=volume_flow,
                                volatility_regime=volatility_regime,
                                event_driven=event_driven,
                                regime_rotation=regime_rotation,
                                trend_breakout=trend_breakout,
                                trend_following=trend_following,
                                ai_result=ai_result,
                                demand_score=demand_score,
                                demand_trend=demand_trend,
                                demand_confidence=demand_confidence,
                                loop_iteration=self.iterations,
                            )
                            sc_log_rows.extend(sym_sc)

                            raw_candidates = apply_regime_filter_with_logs(
                                raw_candidates,
                                symbol=symbol,
                                ai_result=ai_result,
                                ai_pipeline=ai_pipeline,
                                sc_rows=sc_log_rows,
                                loop_iteration=self.iterations,
                            )

                            # D140 — regime-aware confidence weighting,
                            # computed live from market-feature components.
                            raw_candidates = apply_regime_weighting(
                                raw_candidates,
                                symbol=symbol,
                                regime_label=live_regime_label,
                                market_features=live_regime_features or None,
                                min_confidence=regime_min_conf,
                                deployment_pressure=_ctx_deploy_pressure,
                                sc_rows=sc_log_rows,
                                loop_iteration=self.iterations,
                            )

                            # D167.1 — drop edge-gate-BLOCKED sides BEFORE the
                            # anti-churn gate records them, so a doomed short
                            # cannot tombstone a proven long on the same symbol.
                            raw_candidates = self._filter_edge_gate_blocked_raws(
                                raw_candidates,
                                strategies_cfg,
                                symbol=symbol,
                                sc_log_buffer=sc_log_rows,
                            )

                            for raw in raw_candidates:
                                # D122 — stamp dynamic threshold context onto
                                # raw.metadata so the trained meta-labeller +
                                # Wave 9 gate can compute regime / deployment-
                                # aware thresholds per-candidate (no static
                                # numbers downstream).
                                _rm = dict(getattr(raw, "metadata", {}) or {})
                                if dynamic_threshold_ctx:
                                    for _k, _v in dynamic_threshold_ctx.items():
                                        _rm.setdefault(_k, _v)
                                    raw.metadata = _rm
                                cand = self.sig_engine.raw_to_signal_candidate(
                                    raw,
                                    news_score=(ai_result.news_scores.get(symbol) if ai_result else None),
                                    profile_mode=mode_raw,
                                )
                                if cand is None:
                                    _raw_md = dict(getattr(raw, "metadata", {}) or {})
                                    _reason = str(_raw_md.get("_filter_reason") or "news_veto_or_gate")
                                    sc_log_rows.append(
                                        strategy_candidate_row(
                                            symbol=symbol,
                                            strategy=str(raw.strategy),
                                            side=str(raw.side) if raw.side else None,
                                            confidence=raw.confidence,
                                            status="filtered_signal_engine",
                                            reason=_reason,
                                            loop_iteration=self.iterations,
                                            metadata=_raw_md or None,
                                        )
                                    )
                                    continue

                                resolved_ac = asset_class_for_symbol(symbol)
                                if resolved_ac and str(cand.asset_class) != resolved_ac:
                                    cand.asset_class = cast(AssetClass, resolved_ac)

                                if ai_result is not None:
                                    if not isinstance(cand.metadata, dict):
                                        cand.metadata = {}
                                    cand.metadata["ai_macro_regime"] = ai_result.macro_regime
                                    cand.metadata["ai_macro_confidence"] = ai_result.macro_confidence

                                enrich_candidate_volume_z(cand, df)
                                enrich_candidate_liquidity(cand, df)
                                attach_forecast_sequence_history(
                                    cand, df,
                                    enabled=_forecast_sequence_member_enabled(),
                                )
                                batch_candidates.append(cand)
                                sc_log_rows.append(
                                    strategy_candidate_row(
                                        symbol=symbol,
                                        strategy=str(cand.strategy_name),
                                        side=str(cand.side),
                                        confidence=float(cand.confidence),
                                        adjusted_strength=cand.adjusted_signal_strength,
                                        status="generated",
                                        reason="raw_to_signal_candidate",
                                        loop_iteration=self.iterations,
                                    )
                                )

                        # Cross-symbol relative-value opportunities (pairs) are
                        # generated once per cycle after all feature windows load.
                        for pair_raw in pairs_trading.generate_signals(feature_map):
                            # D122 — same dynamic context stamp for pairs.
                            _pm = dict(getattr(pair_raw, "metadata", {}) or {})
                            if dynamic_threshold_ctx:
                                for _k, _v in dynamic_threshold_ctx.items():
                                    _pm.setdefault(_k, _v)
                                pair_raw.metadata = _pm
                            pair_cand = self.sig_engine.raw_to_signal_candidate(
                                pair_raw,
                                news_score=(
                                    ai_result.news_scores.get(pair_raw.symbol)
                                    if ai_result is not None
                                    else None
                                ),
                                profile_mode=mode_raw,
                            )
                            if pair_cand is not None:
                                batch_candidates.append(pair_cand)

                        # Recompute demand with cross-asset anchors once feature
                        # windows are loaded; then apply meta-label filtering.
                        demand_ctx = demand_engine.compute(
                            ai_result=ai_result,
                            feature_map=feature_map,
                        )
                        demand_score = float(demand_ctx.score)
                        demand_trend = str(demand_ctx.trend)
                        demand_confidence = float(demand_ctx.confidence)
                        demand_components = dict(demand_ctx.components or {})
                        threshold_eff = demand_alert_threshold
                        try:
                            if mode_raw in demand_mode_thresholds:
                                threshold_eff = float(demand_mode_thresholds.get(mode_raw))
                        except (TypeError, ValueError):
                            threshold_eff = demand_alert_threshold
                        if (
                            abs(demand_score) >= threshold_eff
                            and (last_demand_alert is None or last_demand_alert.get("trend") != demand_trend)
                        ):
                            last_demand_alert = {
                                "at": datetime.now(timezone.utc).isoformat(),
                                "trend": demand_trend,
                                "score": round(float(demand_score), 6),
                                "confidence": round(float(demand_confidence), 6),
                                "kind": "demand_regime_shift",
                                "threshold": round(float(threshold_eff), 6),
                                "mode": mode_raw,
                            }
                            demand_alert_history.append(dict(last_demand_alert))
                            demand_alert_history = demand_alert_history[-20:]
                        for cand in batch_candidates:
                            md = dict(getattr(cand, "metadata", {}) or {})
                            md["demand_score"] = round(demand_score, 6)
                            md["demand_trend"] = demand_trend
                            md["demand_confidence"] = round(demand_confidence, 6)
                            cand.metadata = md
                        pre_meta_for_label = list(batch_candidates)
                        if meta_enabled and batch_candidates:
                            meta_cfg_eff = dict(meta_cfg)
                            static_bias = dict(meta_cfg_eff.get("strategy_bias", {}) or {})
                            for k, v in meta_dynamic_bias.items():
                                try:
                                    static_bias[k] = float(static_bias.get(k, 0.0)) + float(v)
                                except (TypeError, ValueError):
                                    static_bias[k] = float(v)
                            meta_cfg_eff["strategy_bias"] = static_bias
                            batch_candidates, mlr = meta_filter_candidates(
                                pre_meta_for_label,
                                demand_score=demand_score,
                                cfg=meta_cfg_eff,
                                mode=mode_raw,
                            )
                            if mlr.dropped > 0:
                                logger.info(
                                    "meta_labeling | dropped={} kept={} demand={:.3f}",
                                    mlr.dropped,
                                    mlr.kept,
                                    demand_score,
                                )
                            kept_meta = {(c.symbol, c.strategy_name) for c in batch_candidates}
                            for c in pre_meta_for_label:
                                if (c.symbol, c.strategy_name) in kept_meta:
                                    continue
                                sc_log_rows.append(
                                    strategy_candidate_row(
                                        symbol=c.symbol,
                                        strategy=str(c.strategy_name),
                                        side=str(c.side) if c.side else None,
                                        confidence=float(c.confidence),
                                        adjusted_strength=c.adjusted_signal_strength,
                                        status="filtered_meta",
                                        reason="meta_label_below_threshold",
                                        loop_iteration=self.iterations,
                                    )
                                )

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
                                    ds.metadata = dict(getattr(ds, "metadata", {}) or {})
                                    if dynamic_threshold_ctx:
                                        for _k, _v in dynamic_threshold_ctx.items():
                                            ds.metadata.setdefault(_k, _v)
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

                        # D157 — edge gate: drop candidates from strategies
                        # that have not proven post-cost expectancy in backtest.
                        # Only ever removes capital; gated off by default.
                        batch_candidates = self._apply_edge_gate_filter(
                            batch_candidates, strategies_cfg, sc_log_rows,
                        )

                        generated = len(batch_candidates)
                        executed = 0
                        zero_allocation = Decimal(str(self.capital_pct)) <= 0
                        if batch_candidates or zero_allocation:
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
                            state_equity = _decimal_state_value(
                                portfolio_dict,
                                "portfolio_value",
                                total_equity,
                            )
                            tradable_nav = _decimal_state_value(
                                portfolio_dict,
                                "tradable_capital",
                                state_equity * Decimal(str(self.capital_pct)),
                            )
                            # D156 — portfolio orchestrator. When enabled it
                            # owns rebalancing: net strategy candidates into one
                            # conviction-weighted target per symbol, protect
                            # maturing edge, and skip the global-edge rotation/
                            # recycle tick. Gated off by default → zero change.
                            _orch_cfg = OrchestratorConfig.from_yaml(
                                strategies_cfg.get("portfolio_orchestrator")
                            )
                            if _orch_cfg.enabled and not zero_allocation:
                                # D163 — a-priori edge-Kelly trust: route capital
                                # toward weapons with the strongest PROVEN edge,
                                # derived from the edge-gate verdicts (dynamic,
                                # signal-driven). Empty when the gate is off or
                                # nothing is proven → neutral trust.
                                _edge_kelly: dict[str, Decimal] = {}
                                try:
                                    _eg_thr, _eg_reg = self._load_edge_gate(strategies_cfg)
                                    if _eg_reg is not None:
                                        _edge_kelly = _eg_reg.edge_kelly_trust(
                                            neutral=Decimal("1"),
                                            max_trust=_orch_cfg.max_trust,
                                            min_trust=_orch_cfg.min_trust,
                                        )
                                except Exception as exc:  # noqa: BLE001
                                    self._swallow("edge_kelly_trust", exc)
                                executed = await self._run_orchestrated_tick(
                                    batch_candidates=batch_candidates,
                                    portfolio_dict=portfolio_dict,
                                    tradable=tradable_nav,
                                    mode_raw=mode_raw,
                                    total_equity=state_equity,
                                    session_factory=session_factory,
                                    orch_cfg=_orch_cfg,
                                    sc_log_buffer=sc_log_rows,
                                    strategy_pnl_recent=_strategy_pnl,
                                    resolve_price=_resolve_price_for_symbol,
                                    edge_kelly_trust=_edge_kelly,
                                    ai_result=ai_result,
                                )
                            elif zero_allocation or (self._use_global_edge and not use_legacy):
                                executed, ge_dash_ok = await self._run_global_edge_tick(
                                    batch_candidates=[] if zero_allocation else batch_candidates,
                                    portfolio_dict=portfolio_dict,
                                    tradable=tradable_nav,
                                    mode_raw=mode_raw,
                                    demand_score=demand_score,
                                    demand_trend=demand_trend,
                                    demand_confidence=demand_confidence,
                                    demand_components=demand_components,
                                    demand_alert=last_demand_alert,
                                    demand_alert_history=list(demand_alert_history),
                                    bus=bus,
                                    session_factory=session_factory,
                                    strategies_cfg=strategies_cfg,
                                    symbols=list(symbols),
                                    total_equity=state_equity,
                                    resolve_price=_resolve_price_for_symbol,
                                    strat_cfg=strat_cfg,
                                    sc_log_buffer=sc_log_rows,
                                    strategy_pnl_recent=_strategy_pnl,
                                    ai_result=ai_result,
                                )
                                if ge_dash_ok:
                                    dashboard_snapshot_published = True
                            else:
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
                                    regime.metadata = dict(regime.metadata or {})
                                    regime.metadata["demand_score"] = round(demand_score, 6)
                                    regime.metadata["demand_trend"] = demand_trend
                                    regime.metadata["demand_confidence"] = round(demand_confidence, 6)
                                    regime.metadata["market_volatility"] = float(
                                        demand_components.get("market_volatility", 0.0)
                                    )
                                    regime.metadata["cross_asset_coverage"] = float(
                                        demand_components.get("cross_asset_coverage", 0.0)
                                    )
                                    if last_demand_alert is not None:
                                        regime.metadata["demand_alert"] = dict(last_demand_alert)
                                    if demand_alert_history:
                                        regime.metadata["demand_alert_history"] = list(demand_alert_history[-8:])
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
                                # ── Market-hours decision filter ──────────
                                # Drop opportunities whose venue/asset is
                                # CLOSED right now BEFORE allocation, so
                                # capital is only ever allocated to what is
                                # actually tradeable this cycle (instead of
                                # selecting it and bouncing at the execution
                                # gate, distorting the allocation + spamming
                                # the harvest/stop monitors). Asset class is
                                # derived from the symbol; session truth is
                                # the same authority the execution gate uses.
                                try:
                                    from core.market_session import is_market_open as _mkt_open

                                    _pre = len(opps)
                                    opps = [
                                        o for o in opps
                                        if _mkt_open(asset_class_for_symbol(o.symbol), o.symbol)
                                    ]
                                    _dropped = _pre - len(opps)
                                    if _dropped and self.iterations % 5 == 0:
                                        logger.info(
                                            "market_hours | filtered {} closed-venue "
                                            "opportunit{} from allocation ({} tradeable)",
                                            _dropped,
                                            "y" if _dropped == 1 else "ies",
                                            len(opps),
                                        )
                                except Exception as exc:  # noqa: BLE001
                                    self._swallow("market_hours_opportunity_filter", exc)
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
                                try:
                                    await publish_dashboard_snapshot_d015(
                                        bus,
                                        path="d015",
                                        loop_iteration=self.iterations,
                                        accumulator=self.sig_engine.accumulator if self.sig_engine else None,
                                        regime=regime,
                                        opportunities=opps,
                                        decision=dec,
                                        plan=plan,
                                        portfolio_state=ps_rt,
                                        strategy_pnl_recent=_strategy_pnl,
                                    )
                                    dashboard_snapshot_published = True
                                except Exception as pub_exc:  # noqa: BLE001
                                    logger.warning("dashboard_publish | d015 | {}", pub_exc)
                                for instr in plan.instructions:
                                    px = await _resolve_price_for_symbol(instr.symbol)
                                    ac = _asset_class_lookup(instr.symbol, batch_candidates)
                                    routed = self.router.route(ac, instr.symbol, metadata={"profile_mode": mode_raw})
                                    if routed is None:
                                        continue
                                    imd = instr.metadata if isinstance(instr.metadata, dict) else {}
                                    st_plan = str(imd.get("strategy_name") or "d015_allocator").strip() or "d015_allocator"
                                    sc_log_rows.append(
                                        strategy_candidate_row(
                                            symbol=str(instr.symbol),
                                            strategy=st_plan,
                                            side=str(instr.side) if instr.side else None,
                                            status="selected_for_allocation",
                                            reason="d015_plan_instruction",
                                            loop_iteration=self.iterations,
                                            metadata={
                                                "action": str(instr.action),
                                                "path": "d015",
                                                "target_notional": str(instr.target_notional),
                                            },
                                        )
                                    )
                                    rs = risk_signal_from_execution_instruction(
                                        instr,
                                        signal_id=str(uuid.uuid4()),
                                        broker=routed,
                                        asset_class=ac,
                                        price=px,
                                    )
                                    ok = await _process_signal(
                                        rs,
                                        symbol_hint=instr.symbol,
                                        sc_log_buffer=sc_log_rows,
                                    )
                                    if ok:
                                        executed += 1

                        if not use_legacy and sc_log_rows:
                            record_strategy_candidate_rows(sc_log_rows)
                            c_status = Counter(str(r.get("status", "")) for r in sc_log_rows)
                            c_strat = Counter(str(r.get("strategy", "")) for r in sc_log_rows if r.get("status") == "generated")
                            logger.info(
                                "strategy_candidate_log | cycle | rows={} by_status={} generated_by_strategy_sample={}",
                                len(sc_log_rows),
                                dict(c_status),
                                dict(c_strat.most_common(8)),
                            )
                            await persist_strategy_candidate_rows(session_factory, sc_log_rows)

                    if not dashboard_snapshot_published:
                        try:
                            pd_h = await _load_portfolio_state(
                                session_factory,
                                fallback_portfolio_value=total_equity,
                                signal_price_fallback=None,
                                capital_pct=Decimal(str(self.capital_pct)),
                            )
                            ps_h = portfolio_dict_to_runtime_state(
                                pd_h,
                                mode=mode_raw,
                                capital_pct=float(self.capital_pct),
                            )
                            path_h = "global_edge" if (self._use_global_edge and not use_legacy) else "d015"
                            if use_legacy:
                                reason = "legacy_signal_path"
                                msg = (
                                    "Legacy per-symbol loop — batch allocator snapshot is not emitted. "
                                    "Portfolio row below still refreshes each tick."
                                )
                            elif len(symbols) == 0:
                                reason = "empty_universe"
                                msg = "Universe is empty — check data_pipeline / scanner / DB tier picks."
                            elif symbols_with_features == 0:
                                reason = "no_features"
                                msg = (
                                    "No recent features for scanned symbols — run run_pipeline so "
                                    "feature_snapshots has bars for this timeframe."
                                )
                            elif len(batch_candidates) == 0:
                                reason = "no_batch_candidates"
                                msg = (
                                    "Features exist for at least one symbol but no batch candidates "
                                    "formed (momentum/mean-rev + discovery) under current filters."
                                )
                            else:
                                reason = "publish_failed_or_skipped"
                                msg = (
                                    "Allocator tick ran but the full dashboard snapshot was not written "
                                    "(publish error or coordinator-only path)."
                                )

                            await publish_dashboard_snapshot_heartbeat(
                                bus,
                                path=path_h,
                                loop_iteration=self.iterations,
                                portfolio_state=ps_h,
                                accumulator=self.sig_engine.accumulator if self.sig_engine else None,
                                batch_candidate_count=len(batch_candidates),
                                universe_symbol_count=len(symbols),
                                symbols_with_features=symbols_with_features,
                                symbols_feature_empty=symbols_feature_empty,
                                reason=reason,
                                message=msg,
                            )
                            logger.info(
                                "dashboard_publish | heartbeat | {} | batch_candidates={} symbols_feats={}/{}",
                                reason,
                                len(batch_candidates),
                                symbols_with_features,
                                len(symbols),
                            )
                        except Exception as hb_exc:  # noqa: BLE001
                            logger.warning("dashboard_publish | heartbeat_failed | {}", hb_exc)

                    hb_extra: dict[str, Any] = {"paper_mode": self.paper_mode}
                    hb_extra["demand"] = {
                        "score": round(float(demand_score), 6),
                        "trend": demand_trend,
                        "confidence": round(float(demand_confidence), 6),
                        "components": {
                            k: round(float(v), 6) for k, v in (demand_components or {}).items()
                        },
                        "alert": dict(last_demand_alert) if last_demand_alert is not None else None,
                        "alert_history": list(demand_alert_history[-8:]),
                    }
                    hb_extra["meta_labeling"] = {
                        "dynamic_bias": dict(meta_dynamic_bias),
                        "diagnostics": dict(meta_dynamic_diag),
                    }
                    if self.router is not None:
                        rq = self.router.export_quality_state()
                        hb_extra["routing_quality"] = {
                            "symbols": len((rq.get("quality_map") or {})),
                            "updated_at": rq.get("updated_at"),
                        }
                        if self.iterations % routing_persist_every_n == 0:
                            try:
                                await bus.set_state("routing.quality.state", rq)
                            except Exception:  # noqa: BLE001
                                pass
                    if ai_pipeline is not None and getattr(ai_pipeline, "classifier", None) is not None:
                        _cls = ai_pipeline.classifier
                        ai_status = _cls.runtime_ai_status()
                        if ai_result is not None and isinstance(getattr(ai_result, "news_feed_status", None), dict):
                            ai_status = {**ai_status, **ai_result.news_feed_status}
                        hb_extra["ai"] = ai_status
                    else:
                        hb_extra["ai"] = {"kind": "off", "ai_degraded": False}

                    await publish_runner_heartbeat(
                        bus, runner_name="orchestrator",
                        symbols=symbols, generated=generated, executed=executed,
                        extra=hb_extra,
                    )
                    # Global mark-to-market sweep: refresh every open position's
                    # ``unrealised_pnl`` from the latest feature snapshot close,
                    # so the ``daily_pnl_unrealised_differs_from_open_book``
                    # accounting check converges even when the allocator hasn't
                    # touched a given symbol this cycle (notably after a crash
                    # recovery where inherited positions stay stale otherwise).
                    try:
                        refreshed = await _refresh_position_marks_and_persist(
                            session_factory,
                            timeframe=self.timeframe,
                            price_oracle=self._live_price_oracle,
                        )
                        if refreshed:
                            logger.debug(
                                "mark_to_market | refreshed {} positions",
                                refreshed,
                            )
                    except Exception as mtm_exc:  # noqa: BLE001
                        logger.warning("mark_to_market | sweep failed (non-fatal) | {}", mtm_exc)
                    self.last_iteration_at = datetime.now(timezone.utc)
                    # Remember the count so the adaptive_mode classifier on
                    # the NEXT tick can use signal density as an input.
                    self._last_generated_count = int(generated or 0)
                    self.iterations += 1
                    self.last_error = None
                    if self._swallow_iter_total:
                        logger.info(
                            "trading_loop | iteration #{} | generated={} executed={} | swallowed={} {}",
                            self.iterations, generated, executed,
                            self._swallow_iter_total, dict(self._swallow_counts),
                        )
                    else:
                        logger.info(
                            "trading_loop | iteration #{} | generated={} executed={}",
                            self.iterations, generated, executed,
                        )
                    self._swallow_iter_total = 0
                    self._swallow_counts = {}

                except Exception as exc:
                    self.last_error = str(exc)[:300]
                    logger.exception("trading_loop | iteration failed: {}", exc)

                    if owns_engine:
                        try:
                            await dispose_engine(engine)
                        except Exception:
                            pass
                        engine, session_factory = await init_async_database()
                        owns_engine = engine is not None
                        if session_factory is None:
                            logger.error("trading_loop | DB reconnect failed — will retry next iteration")
                        else:
                            bus = CommandBus(session_factory)
                            self._control_bus = bus
                    else:
                        bus = CommandBus(session_factory)
                        self._control_bus = bus
                        logger.warning(
                            "trading_loop | iteration DB error — rebound CommandBus (shared engine; not disposed)",
                        )

                # Adaptive cadence (Phase 1) — the loop paces itself to the
                # market's actual signal rate, the session window, and the
                # current adaptive_mode label, rather than a static per-mode
                # number. The mode_cadence_map is kept as the ``base_interval``
                # input so operators can still influence the centre point via
                # YAML, but the dynamic factors dominate.
                current_mode = self._read_active_mode()
                base_iter = mode_cadence_map.get(current_mode, self.loop_interval_sec)
                try:
                    from system.adaptive_cadence import CadenceInputs, compute_loop_cadence
                    iter_interval = compute_loop_cadence(
                        CadenceInputs(
                            mode=current_mode,
                            recent_signal_density=float(getattr(self, "_last_generated_count", 0) or 0),
                            base_interval_sec=float(base_iter),
                        )
                    )
                    if self.iterations % 5 == 0:
                        logger.info(
                            "adaptive_cadence | mode={} density={} base={}s → next={}s",
                            current_mode,
                            getattr(self, "_last_generated_count", 0),
                            int(base_iter),
                            int(iter_interval),
                        )
                except Exception as exc:  # noqa: BLE001
                    logger.debug("adaptive_cadence | failed, falling back to static: {}", exc)
                    iter_interval = base_iter

                # Force discovery if capital is under-allocated
                try:
                    _current_pd = await _load_portfolio_state(
                        session_factory,
                        fallback_portfolio_value=total_equity,
                        signal_price_fallback=None,
                        capital_pct=Decimal(str(self.capital_pct)),
                    )
                    await self._check_and_trigger_unallocated_capital_discovery(
                        portfolio_dict=_current_pd,
                        total_equity=total_equity,
                        executed_count=executed if "executed" in locals() else 0,
                    )
                except Exception as fd_exc:  # noqa: BLE001
                    logger.warning("trading_loop | forced discovery check failed (non-fatal): {}", fd_exc)

                try:
                    should_stop = await self._wait_for_next_iteration(iter_interval)
                    if should_stop:
                        break
                except asyncio.TimeoutError:
                    pass
                except asyncio.CancelledError:
                    break

        except asyncio.CancelledError:
            logger.info("trading_loop | cancelled")
        except Exception as exc:
            self.last_error = str(exc)[:300]
            logger.exception("trading_loop | fatal: {}", exc)
        finally:
            self._running = False
            set_execution_engine(None)
            if owns_engine and engine is not None:
                try:
                    await dispose_engine(engine)
                except Exception:
                    pass

    def _ensure_arb_stack(self, strategies_cfg: dict[str, Any]) -> None:
        if self._arb_stack is not None:
            return
        fcfg = dict(strategies_cfg.get("funding_rate_arbitrage") or {})
        ccfg = dict(strategies_cfg.get("cross_exchange_arbitrage") or {})
        reg = CapabilityRegistry(logger=logger)
        reg.load_from_config(strategies_cfg.get("arbitrage_capabilities") or {})

        async def _broker_getter(name: str) -> Any:
            if self._broker_manager and name in getattr(self._broker_manager, "adapters", {}):
                return self._broker_manager.adapters[name]
            return None

        prov = FundingRateDataProvider(_broker_getter, logger=logger, liquidity_tracker=LiquidityTracker())
        vs = VenueSelector(
            reg,
            prov,
            logger,
            fcfg,
            latency_predictor=self._latency_predictor,
        )
        self._arb_stack = {
            "registry": reg,
            "provider": prov,
            "venue_selector": vs,
            "funding": FundingRateArbitrageStrategy(fcfg, vs, logger=logger),
            "cross": CrossExchangeArbitrageStrategy(reg, prov, ccfg, logger=logger),
            "fcfg": fcfg,
            "ccfg": ccfg,
        }

    def _load_edge_gate(
        self, strategies_cfg: dict[str, Any]
    ) -> tuple[EdgeGateThresholds, "EdgeGateRegistry | None"]:
        """Load edge-gate thresholds + verdict registry (cached, refreshed by mtime).

        Returns ``(thresholds, registry|None)``. When disabled, registry is
        None and the caller treats every strategy as allowed.
        """
        thr = EdgeGateThresholds.from_yaml(strategies_cfg.get("edge_gate"))
        if not thr.enabled:
            return thr, None
        from backtest.edge_gate import DEFAULT_REGISTRY_PATH
        eg_cfg = strategies_cfg.get("edge_gate") or {}
        path = Path(str(eg_cfg.get("state_path") or DEFAULT_REGISTRY_PATH))
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = 0.0
        cached = getattr(self, "_edge_gate_cache", None)
        if cached is not None and cached[0] == mtime:
            return thr, cached[1]
        reg = EdgeGateRegistry(path).load()
        self._edge_gate_cache = (mtime, reg)
        return thr, reg

    def _filter_edge_gate_blocked_raws(
        self,
        raw_candidates: list[Any],
        strategies_cfg: dict[str, Any],
        *,
        symbol: str,
        sc_log_buffer: list[dict[str, Any]] | None,
    ) -> list[Any]:
        """D167.1 — drop edge-gate-BLOCKED (strategy, side) RAW candidates BEFORE
        the signal engine's anti-churn gate records them.

        Why this must happen pre-anti-churn (the bug it fixes): anti-churn's
        cross-strategy contradiction tombstones a symbol the moment it sees a
        long and a short on it. A short side the edge gate has BLOCKED (no
        proven edge / negative expectancy, e.g. mean_reversion#short PF<1 from
        D161) can never trade — yet if it reaches anti-churn first it poisons a
        PROVEN long on the same symbol (observed live: mean_reversion short
        USDJPY tombstoning trend_breakout long USDJPY, PF 4.11, 100% dropped).
        Removing dead-on-arrival sides here keeps the contradiction analysis to
        live, tradeable candidates only. ``reduced``/``allowed`` sides pass
        through unchanged (their confidence scaling still happens later in
        :meth:`_apply_edge_gate_filter`). Gated off → returns the list unchanged.
        """
        thr, reg = self._load_edge_gate(strategies_cfg)
        if reg is None:  # disabled
            return raw_candidates
        kept: list[Any] = []
        for raw in raw_candidates:
            strat = str(getattr(raw, "strategy", "") or "")
            raw_side = str(getattr(raw, "side", "") or "long").strip().lower()
            side = "short" if raw_side in ("short", "sell", "s") else "long"
            if reg.is_blocked(strat, thr, side=side):
                if sc_log_buffer is not None:
                    try:
                        sc_log_buffer.append(
                            strategy_candidate_row(
                                symbol=symbol,
                                strategy=strat,
                                side=str(getattr(raw, "side", "") or "") or None,
                                confidence=float(getattr(raw, "confidence", 0) or 0),
                                status="edge_gate_blocked",
                                reason=f"{side} side not edge-proven; dropped pre-anti-churn (D167.1)",
                                loop_iteration=self.iterations,
                                metadata={"side": side, "stage": "pre_anti_churn"},
                            )
                        )
                    except Exception as exc:  # noqa: BLE001
                        self._swallow("edge_gate_raw_block_log", exc)
                continue
            kept.append(raw)
        return kept

    def _apply_edge_gate_filter(
        self,
        batch_candidates: list[Any],
        strategies_cfg: dict[str, Any],
        sc_log_buffer: list[dict[str, Any]] | None,
    ) -> list[Any]:
        """Drop candidates whose strategy is ``blocked`` by the edge gate.

        D161 — verdicts are PER SIDE: a candidate's entry side (long/short)
        selects which verdict applies, because edge is side-specific
        (mean_reversion's long side is proven, its short side measured
        PF 0.66). Sell-side ENTRIES from a short-blocked strategy are dropped;
        closes/reduce-only orders never pass through this filter (they are
        generated downstream by the orchestrator from book state).

        Gated off by default (returns the list unchanged). ``reduced`` /
        ``allowed`` strategies pass through here; their size multiplier is
        applied later via the orchestrator trust prior. Cross-sectional /
        news strategies the gate cannot backtest follow ``unproven_policy``.
        """
        thr, reg = self._load_edge_gate(strategies_cfg)
        if reg is None:  # disabled
            return batch_candidates
        kept: list[Any] = []
        blocked_counts: dict[str, int] = {}
        reduced_counts: dict[str, int] = {}
        for cand in batch_candidates:
            strat = str(getattr(cand, "strategy_name", "") or "")
            cand_side = str(getattr(cand, "side", "") or "long").strip().lower()
            side = "short" if cand_side in ("short", "sell", "s") else "long"
            if reg.is_blocked(strat, thr, side=side):
                key = f"{strat}:{side}"
                blocked_counts[key] = blocked_counts.get(key, 0) + 1
                if sc_log_buffer is not None:
                    try:
                        sc_log_buffer.append(
                            strategy_candidate_row(
                                symbol=str(getattr(cand, "symbol", "") or ""),
                                strategy=strat,
                                side=str(getattr(cand, "side", "") or ""),
                                confidence=float(getattr(cand, "confidence", 0) or 0),
                                status="edge_gate_blocked",
                                reason=f"{side} side not edge-proven (D157/D161)",
                                loop_iteration=self.iterations,
                                metadata={"side": side},
                            )
                        )
                    except Exception as exc:  # noqa: BLE001
                        self._swallow("edge_gate_block_log", exc)
                continue
            # Non-blocked: apply the a-priori size multiplier (1.0 for proven,
            # reduced_multiplier for weak/unproven) by scaling confidence so the
            # down-weight propagates to EVERY downstream path (sizing + the D156
            # orchestrator netting), not only the orchestrator.
            mult = reg.size_multiplier_for(strat, thr, side=side)
            if mult < Decimal("1") and mult > 0:
                try:
                    cand.confidence = Decimal(str(cand.confidence)) * mult
                    cand.adjusted_signal_strength = Decimal(str(cand.adjusted_signal_strength)) * mult
                    if isinstance(getattr(cand, "metadata", None), dict):
                        cand.metadata["edge_gate_multiplier"] = float(mult)
                    reduced_counts[strat] = reduced_counts.get(strat, 0) + 1
                except Exception as exc:  # noqa: BLE001
                    self._swallow("edge_gate_reduce_scale", exc)
            kept.append(cand)
        if blocked_counts:
            logger.info("edge_gate | blocked candidates by strategy: {}", blocked_counts)
        if reduced_counts:
            logger.info("edge_gate | reduced candidates by strategy: {}", reduced_counts)
        return kept

    @staticmethod
    def _orchestrator_strategy_trust(
        strategy_pnl_recent: dict[str, dict[str, Any]] | None,
        cfg: OrchestratorConfig,
        edge_prior: dict[str, Decimal] | None = None,
    ) -> dict[str, Decimal]:
        """Resolve per-strategy trust = a-priori PROVEN edge x posterior live P&L.

        Two signals compose into one capital-routing weight (D163):

          * ``edge_prior`` — the a-priori edge-gate Kelly trust (capital flows
            to the weapon with the strongest *proven* post-cost edge). This is
            the operating point; the orchestrator's ``min_trust``/``max_trust``
            are only the safety bounds it is normalised into.
          * ``strategy_pnl_recent`` — the posterior: a strategy bleeding
            recently is tilted down, a profitable one up. This stops a
            persistently-losing layer (the D156 rotation bleed) from
            dominating even if its backtest looked fine.

        Combined = clamp(prior x posterior_tilt, min_trust, max_trust). With no
        edge prior the prior defaults to neutral 1.0 (pure posterior behaviour,
        backward compatible). Neutral (1.0) when neither signal has a sample.

        D166 (Phase 2) — the scoreboard gates size increases:
          * The posterior is only trusted once a weapon has at least
            ``cfg.min_live_closes_for_posterior`` live CLOSES — one noisy fill
            cannot flip trust (below the floor → prior only).
          * Once enough live closes exist AND live net P&L is negative, trust
            is capped at neutral (1.0): live evidence overrides an optimistic
            backtest prior, so a proven-live LOSER is never AMPLIFIED above
            neutral (it can still trade; the edge gate / risk layer own
            blocking — this layer only governs amplification).
        """
        one = Decimal("1")
        edge_prior = edge_prior or {}
        min_closes = int(getattr(cfg, "min_live_closes_for_posterior", 0) or 0)
        names = set(edge_prior) | {
            str(n) for n in (strategy_pnl_recent or {})
        }
        out: dict[str, Decimal] = {}
        for name in names:
            base = edge_prior.get(name, one)
            tilt = one
            live_net = Decimal("0")
            live_closes = 0
            stats = (strategy_pnl_recent or {}).get(name)
            if isinstance(stats, dict):
                try:
                    live_net = Decimal(str(stats.get("net_pnl", 0) or 0))
                    live_closes = int(stats.get("fills", 0) or 0)
                except Exception:  # noqa: BLE001
                    live_net, live_closes = Decimal("0"), 0
                # Only trust the posterior with enough live evidence.
                if live_closes >= max(1, min_closes):
                    # Bounded tilt: scale net P&L per fill into a modest factor.
                    per_fill = live_net / Decimal(live_closes)
                    if per_fill >= 0:
                        tilt = one + min(cfg.max_trust - one, per_fill / Decimal("100"))
                    else:
                        tilt = one + max(cfg.min_trust - one, per_fill / Decimal("100"))
            combined = base * tilt
            # Scoreboard cap: a weapon proven to LOSE live is never amplified.
            if min_closes > 0 and live_closes >= min_closes and live_net < 0 and combined > one:
                combined = one
            out[name] = max(cfg.min_trust, min(cfg.max_trust, combined))
        return out

    async def _run_orchestrated_tick(
        self,
        *,
        batch_candidates: list[Any],
        portfolio_dict: dict[str, Any],
        tradable: Decimal,
        mode_raw: str,
        total_equity: Decimal,
        session_factory: Any,
        orch_cfg: OrchestratorConfig,
        sc_log_buffer: list[dict[str, Any]] | None = None,
        strategy_pnl_recent: dict[str, dict[str, Any]] | None = None,
        resolve_price: Any = None,
        edge_kelly_trust: dict[str, Decimal] | None = None,
        ai_result: Any | None = None,
    ) -> int:
        """D156 portfolio-orchestrator path.

        Nets every strategy candidate for a symbol into ONE conviction-weighted
        target, sizes by conviction, manages net exposure, and protects
        maturing edge — then routes the resulting minimal order set through the
        SAME risk + execution path as every other order (``_process_signal_global``).
        Never bypasses risk. Returns the executed order count.

        When this path runs, the global-edge rotation/recycle tick is skipped
        (the orchestrator now owns rebalancing).
        """
        if self.sig_engine is None or self.risk_engine is None or self.execution_engine is None:
            return 0
        # Refresh the book with live broker positions so edge-protection and
        # netting diff against reality, not a stale snapshot.
        if self._broker_manager:
            try:
                await asyncio.wait_for(
                    _merge_live_broker_positions_into_portfolio_state(
                        portfolio_dict, self._broker_manager, paper_mode=self.paper_mode,
                    ),
                    timeout=float(os.getenv("BROKER_POSITION_MERGE_TIMEOUT_SEC", "20")),
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("orchestrator | broker position merge timeout/error | {}", exc)

        intents = build_intents_from_candidates(batch_candidates)
        # Build the current book from portfolio_dict positions.
        book: list[BookPosition] = []
        for sym, p in (portfolio_dict.get("positions") or {}).items():
            try:
                qty = Decimal(str(p.get("quantity", "0") or "0"))
                if qty == 0:
                    continue
                avg = Decimal(str(p.get("avg_entry_price", "0") or "0"))
                cur = Decimal(str(p.get("current_price", avg) or avg))
                asset_class = str(p.get("asset_class", "") or "")
                symbol = str(p.get("symbol", sym))
                unreal = unrealised_pnl_account_currency(
                    symbol=symbol,
                    asset_class=asset_class,
                    quantity=qty,
                    avg_entry_price=avg,
                    current_price=cur,
                )
                book.append(
                    BookPosition(
                        symbol=symbol,
                        signed_qty=qty,
                        avg_price=avg,
                        current_price=cur,
                        asset_class=asset_class,
                        broker=str(p.get("broker", "") or ""),
                        holding_sec=Decimal(str(p.get("holding_sec", "0") or "0")),
                        unrealised_pnl=unreal,
                    )
                )
            except Exception:  # noqa: BLE001
                continue

        # Inject the live per-strategy trust (edge-gate prior × recent P&L)
        # while preserving every other config field — including the D158
        # Phase 2 temperament/threat blocks. ``replace`` keeps this robust as
        # new config fields are added.
        from dataclasses import replace as _dc_replace
        cfg = _dc_replace(
            orch_cfg,
            strategy_trust=self._orchestrator_strategy_trust(
                strategy_pnl_recent, orch_cfg, edge_prior=edge_kelly_trust,
            ),
        )
        result = orchestrate(intents, book, nav=total_equity, mode=mode_raw, config=cfg)
        logger.info(
            "orchestrator | intents={} book={} orders={} netted={} conflicts={} protected={} "
            "flips={} opens={} closes={} reduces={} increases={} suppressed={} net_target={}",
            result.diagnostics.get("intent_count"), result.diagnostics.get("book_positions"),
            len(result.orders), result.diagnostics.get("netted_symbols"),
            result.diagnostics.get("conflicts_resolved"), result.diagnostics.get("protected_positions"),
            result.diagnostics.get("flips"), result.diagnostics.get("opens"),
            result.diagnostics.get("closes"), result.diagnostics.get("reduces"),
            result.diagnostics.get("increases"), result.diagnostics.get("suppressed_rebalances"),
            result.diagnostics.get("net_target"),
        )

        # Price map for quantity sizing: the signal engine converts the
        # orchestrator's target NOTIONAL into a share quantity via
        # ``quantity = notional / price``. Without a price the engine falls
        # back to a wrong default, producing wildly oversized orders (e.g. a
        # GLD order sized against $1.60 instead of $398 → 20× NAV, then
        # rejected by the execution pre-check). Use the live book price where
        # we have it, else resolve the latest close.
        book_px = {p.symbol: p.current_price for p in book if p.current_price > 0}
        executed = 0
        for od in result.orders:
            try:
                px = book_px.get(od.symbol)
                if (px is None or px <= 0) and resolve_price is not None:
                    try:
                        px = await resolve_price(od.symbol)
                    except Exception:  # noqa: BLE001
                        px = None
                if px is None or px <= 0:
                    logger.debug("orchestrator | no price for {} — skipping order", od.symbol)
                    continue
                is_reduce = bool(od.reduce_only or od.close_only)
                md: dict[str, Any] = {
                    "close": str(px),
                    "orchestrator": True,
                    "orchestrator_reason": od.reason,
                    "net_conviction": str(od.net_conviction),
                    "contributing_strategies": ",".join(od.contributing[:12]),
                    "confidence": str(min(Decimal("0.95"), max(Decimal("0.3"), abs(od.net_conviction)))),
                    "asset_class": od.asset_class or "equity",
                }
                if od.broker:
                    md["broker"] = od.broker
                if is_reduce:
                    # trim_symbol path: side encodes the EXISTING position side
                    # being reduced (sell→reduce a long, buy→reduce a short).
                    md["side"] = "long" if od.side == "sell" else "short"
                    md["reduce_only"] = True
                    if not od.close_only:
                        md["partial_reduce_only"] = True
                    kind = "trim_symbol"
                else:
                    md["side"] = "long" if od.side == "buy" else "short"
                    kind = "open_strategy"
                action = CoordinatorAction(
                    kind=kind,
                    symbol=od.symbol,
                    strategy_name="portfolio_orchestrator",
                    capital=od.delta_notional,
                    priority_score=abs(od.net_conviction),
                    metadata=md,
                )
                sig = process_coordinator_action(
                    action,
                    self.sig_engine,
                    portfolio_value=tradable,
                    news_score=_news_score_for_symbol(ai_result, od.symbol),
                )
                if sig is None:
                    continue
                block_reason, block_meta = await self._preflight_built_signal(
                    sig,
                    action,
                    portfolio_dict=portfolio_dict,
                    session_factory=session_factory,
                )
                if block_reason:
                    if sc_log_buffer is not None:
                        sc_log_buffer.append(
                            strategy_candidate_row(
                                symbol=str(getattr(sig, "symbol", "") or ""),
                                strategy=str(getattr(sig, "strategy", "") or ""),
                                side=str(getattr(sig, "side", "") or ""),
                                confidence=float(getattr(sig, "confidence", 0) or 0),
                                status="filtered_preflight_viability",
                                reason=block_reason,
                                loop_iteration=self.iterations,
                                metadata={
                                    "kind": str(getattr(action, "kind", "") or ""),
                                    "priority_score": str(getattr(action, "priority_score", "") or ""),
                                    "orchestrator_path": "portfolio_orchestrator",
                                    **block_meta,
                                },
                            )
                        )
                    logger.info(
                        "orchestrator | preflight rejected {} {} | reason={} meta={}",
                        getattr(sig, "symbol", od.symbol),
                        getattr(sig, "side", od.side),
                        block_reason,
                        block_meta,
                    )
                    continue
                ok = await self._process_signal_global(
                    sig, session_factory, portfolio_dict, total_equity, tradable,
                    sc_log_buffer=sc_log_buffer,
                )
                if ok:
                    executed += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning("orchestrator | order routing failed for {} | {}", od.symbol, exc)
                continue
        return executed

    async def _run_global_edge_tick(
        self,
        *,
        batch_candidates: list[Any],
        portfolio_dict: dict[str, Any],
        tradable: Decimal,
        mode_raw: str,
        demand_score: float,
        demand_trend: str,
        demand_confidence: float,
        demand_components: dict[str, Any],
        demand_alert: dict[str, Any] | None,
        demand_alert_history: list[dict[str, Any]],
        bus: CommandBus,
        session_factory: Any,
        strategies_cfg: dict[str, Any],
        symbols: list[str],
        total_equity: Decimal,
        resolve_price,
        strat_cfg: dict[str, Any],
        sc_log_buffer: list[dict[str, Any]] | None = None,
        strategy_pnl_recent: dict[str, dict[str, Any]] | None = None,
        ai_result: Any | None = None,
    ) -> tuple[int, bool]:
        """Global edge coordinator path: treasury, arb scans, ranked actions → risk → execution.

        Returns ``(executed_count, dashboard_snapshot_written)``.
        """
        self._ensure_arb_stack(strategies_cfg)
        assert self._treasury is not None
        assert self.sig_engine is not None
        assert self.execution_engine is not None

        if self._broker_manager:
            try:
                await asyncio.wait_for(
                    self._treasury.refresh(self._broker_manager),
                    timeout=float(os.getenv("TREASURY_REFRESH_TIMEOUT_SEC", "20")),
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("global_edge | treasury refresh timeout/error | {}", exc)
        merge_treasury_into_portfolio_state(portfolio_dict, self._treasury)
        if self._broker_manager:
            try:
                await asyncio.wait_for(
                    _merge_live_broker_positions_into_portfolio_state(
                        portfolio_dict,
                        self._broker_manager,
                        paper_mode=self.paper_mode,
                    ),
                    timeout=float(os.getenv("BROKER_POSITION_MERGE_TIMEOUT_SEC", "20")),
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("global_edge | broker position merge timeout/error | {}", exc)

        try:
            pmeta = portfolio_dict.get("metadata")
            if not isinstance(pmeta, dict):
                pmeta = {}
                portfolio_dict["metadata"] = pmeta
            dyn_meta = pmeta.get("dynamic_thresholds")
            if not isinstance(dyn_meta, dict):
                dyn_meta = {}
                pmeta["dynamic_thresholds"] = dyn_meta
            per_strategy_meta: dict[str, dict[str, Any]] = {}
            for name, stats in (strategy_pnl_recent or {}).items():
                if not isinstance(stats, dict):
                    continue
                per_strategy_meta[str(name)] = {
                    "recent_net_pnl": stats.get("net_pnl", 0),
                    "recent_fills": stats.get("fills", 0),
                    "recent_win_rate": stats.get("win_rate", 0),
                    "sample_target_notional": stats.get("sample_target_notional", 0),
                }
            if per_strategy_meta:
                dyn_meta["per_strategy"] = per_strategy_meta
        except Exception as exc:  # noqa: BLE001
            logger.debug("global_edge | risk strategy-performance metadata failed: {}", exc)

        # The operator's capital allocation slider is the deployment target.
        # Static position ceilings are legacy rails and are only applied when
        # ``enforce_static_exposure_caps`` is explicitly enabled.
        risk_cfg = getattr(self.risk_engine, "config", {}) or {}
        enforce_static_exposure_caps = bool(risk_cfg.get("enforce_static_exposure_caps", False))
        max_pos_pct = Decimal("1.0")
        if enforce_static_exposure_caps:
            try:
                max_pos_pct = Decimal(str(risk_cfg.get("max_position_pct", "1.0")))
            except Exception:  # noqa: BLE001
                max_pos_pct = Decimal("1.0")
            if os.getenv("USE_ADAPTIVE_SIZING", "1").strip().lower() in ("1", "true", "yes", "on"):
                try:
                    _mo = (risk_cfg.get("mode_overrides") or {}).get(mode_raw or "trader") or {}
                    _override = _mo.get("max_position_pct")
                    if _override is not None:
                        max_pos_pct = Decimal(str(_override))
                except Exception:  # noqa: BLE001
                    pass

        held = held_positions_from_portfolio(
            portfolio_dict,
            decay=Decimal(str(self._global_edge_cfg.get("held_edge_decay_per_day", "0.08"))),
            nav=total_equity,
            max_position_pct=max_pos_pct,
        )
        new_opps: list[Any] = []
        pos_pct = Decimal(str(strategies_cfg.get("signal_engine", {}).get("default_position_pct", "0.05")))
        for cand in batch_candidates:
            try:
                px = await resolve_price(cand.symbol)
            except Exception:  # noqa: BLE001
                px = Decimal("0")
            so = signal_candidate_to_strategy_opportunity(
                cand,
                nav=tradable,
                position_pct=pos_pct,
                price=px,
                max_position_pct=max_pos_pct,
            )
            if so is not None:
                new_opps.append(so)

        stack = self._arb_stack or {}
        fcfg = stack.get("fcfg") or {}
        ccfg = stack.get("ccfg") or {}
        boost_f = Decimal(str(self._global_edge_cfg.get("arbitrage_edge_boost", {}).get("funding_rate_arbitrage", "0.01")))
        boost_x = Decimal(str(self._global_edge_cfg.get("arbitrage_edge_boost", {}).get("cross_exchange_arbitrage", "0.015")))
        notional = Decimal(str(fcfg.get("min_liquidity_notional", "5000")))

        if tradable > 0 and self._enable_arbitrage and fcfg.get("enabled"):
            funding = stack.get("funding")
            if funding is not None:
                for sym in fcfg.get("symbols") or []:
                    fs = await funding.evaluate_symbol(sym, notional)
                    if fs is not None:
                        new_opps.append(funding_arb_signal_to_strategy_opportunity(fs, capital=notional, edge_boost=boost_f))
                        log_arb_event("detect", strategy="funding_rate_arbitrage", symbol=sym)

        if tradable > 0 and self._enable_arbitrage and ccfg.get("enabled"):
            cross = stack.get("cross")
            if cross is not None:
                for sym in ccfg.get("symbols") or []:
                    d = await cross.evaluate_symbol(sym, notional)
                    if isinstance(d, dict):
                        new_opps.append(cross_exchange_dict_to_strategy_opportunity(d, capital=notional, edge_boost=boost_x))
                        log_arb_event("detect", strategy="cross_exchange_arbitrage", symbol=sym)

        held_direction: dict[str, str] = {}
        for h in held:
            md = getattr(h, "metadata", None)
            qty = Decimal("0")
            if isinstance(md, dict):
                try:
                    qty = Decimal(str(md.get("quantity", "0") or "0"))
                except Exception:  # noqa: BLE001
                    qty = Decimal("0")
            sym_key = str(getattr(h, "symbol", "") or "").strip().upper()
            if sym_key and qty != 0:
                held_direction[sym_key] = "long" if qty > 0 else "short"

        adaptive_runtime_on = os.getenv("USE_ADAPTIVE_SIZING", "1").strip().lower() in (
            "1",
            "true",
            "yes",
            "on",
        )

        if held_direction and not (adaptive_runtime_on and tradable > 0):
            filtered_existing: list[Any] = []
            skipped_existing: list[str] = []
            for opp in new_opps:
                sym_key = str(getattr(opp, "symbol", "") or "").strip().upper()
                side_raw = str(getattr(opp, "side", "") or "").strip().lower()
                opp_dir = "long" if side_raw in {"long", "buy"} else "short" if side_raw in {"short", "sell"} else ""
                if sym_key and opp_dir and held_direction.get(sym_key) == opp_dir:
                    skipped_existing.append(sym_key)
                    continue
                filtered_existing.append(opp)
            if skipped_existing:
                logger.info(
                    "trading_loop | coord | skipped {} opportunities already held same direction | sample={}",
                    len(skipped_existing),
                    skipped_existing[:8],
                )
            new_opps = filtered_existing

        # Audit #11: age-out STALE resting orders before the short-circuit.
        # A limit order that never fills (PASSIVE/LIMIT urgency on delayed
        # IBKR data) would otherwise stay "working" forever and perpetually
        # exclude its symbol below, silently starving that opportunity. Cancel
        # anything older than the threshold so the symbol becomes tradable
        # again; fresh orders (still likely to fill) are left alone.
        try:
            _stale_sec = float(os.getenv("STALE_WORKING_ORDER_SEC", "900") or 900)
        except (TypeError, ValueError):
            _stale_sec = 900.0
        if _stale_sec > 0 and self.execution_engine is not None:
            try:
                _stale_cancelled = await self.execution_engine.cancel_working_orders(
                    session_factory=session_factory,
                    reason="stale_working_order_ageout",
                    older_than_sec=_stale_sec,
                )
                if _stale_cancelled:
                    logger.info(
                        "trading_loop | aged-out {} stale working order(s) (>{}s) so their symbols are tradable again",
                        _stale_cancelled, int(_stale_sec),
                    )
            except Exception as exc:  # noqa: BLE001
                self._swallow("stale_working_order_ageout", exc)

        working_keys = await _load_working_order_keys(session_factory)
        if working_keys:
            filtered_opps: list[Any] = []
            skipped_symbols: list[str] = []
            for opp in new_opps:
                side_raw = (getattr(opp, "side", "") or "").strip().lower()
                if side_raw in _DIRECTIONAL_SIDES:
                    sym_key = str(getattr(opp, "symbol", "")).strip().upper()
                    order_side = _SIDE_TO_ORDER_SIDE.get(side_raw, side_raw)
                    if sym_key and (sym_key, order_side) in working_keys:
                        skipped_symbols.append(sym_key)
                        continue
                filtered_opps.append(opp)
            if skipped_symbols:
                logger.info(
                    "trading_loop | coord | skipped {} opportunities with live working orders | sample={}",
                    len(skipped_symbols),
                    skipped_symbols[:8],
                )
            new_opps = filtered_opps

        new_opps_dedup, lost_to_winners = dedupe_opportunities_by_symbol(new_opps)
        buf = sc_log_buffer
        if buf is not None and lost_to_winners:
            for loser, winner in lost_to_winners:
                try:
                    ls = getattr(loser, "priority_score", None)
                    ws = getattr(winner, "priority_score", None)
                    buf.append(
                        strategy_candidate_row(
                            symbol=str(loser.symbol),
                            strategy=str(loser.strategy_name),
                            side=str(getattr(loser, "side", "") or ""),
                            confidence=float(getattr(loser, "confidence", 0) or 0),
                            status="lost_to_strategy",
                            reason="same_symbol_dedupe",
                            winner_strategy=str(winner.strategy_name),
                            loop_iteration=self.iterations,
                            metadata={
                                "loser_score": str(ls) if ls is not None else None,
                                "winner_score": str(ws) if ws is not None else None,
                            },
                        )
                    )
                except Exception:  # noqa: BLE001
                    pass

        # Phase 2: inject cost-aware adaptive inputs into the coordinator
        # config so its ``_threshold(mode)`` returns a dynamic value
        # grounded in live execution cost + recent realised outcomes.
        # We patch a transient ``adaptive_edge`` block on the cfg dict;
        # the coordinator's threshold reads it if present, falls back
        # cleanly otherwise.
        adaptive_edge_cfg: dict[str, Any] = {}
        try:
            from system.adaptive_edge import estimate_cross_venue_cost_bps
            if self.execution_engine is not None and self.execution_engine._wave9_cfg is not None:
                w9 = self.execution_engine._wave9_cfg
                active_brokers = list(self.available_brokers or [])
                # Conservative asset-class set: equity + crypto covers the
                # bulk of volume; the average is a coarse baseline that the
                # coordinator's per-symbol decisions further refine.
                active_acs = ["equity", "crypto"]
                cost_bps = estimate_cross_venue_cost_bps(
                    venue_priors=w9.venue_priors,
                    slippage_model=w9.slippage_model,
                    active_brokers=active_brokers,
                    active_asset_classes=active_acs,
                )
                if cost_bps is not None:
                    adaptive_edge_cfg["cross_venue_cost_bps"] = cost_bps
        except Exception as exc:  # noqa: BLE001
            self._swallow("adaptive_edge_cost_estimate", exc)
        # Outcome inputs: compute a coarse recent win-rate / avg return
        # from the persisted P&L. Use today's daily_pnl row for now —
        # Phase 4 will move this to a rolling per-strategy window.
        try:
            real = Decimal(str(portfolio_dict.get("daily_realized_pnl", 0) or 0))
            trades = int(portfolio_dict.get("trades_today", 0) or 0)
            if trades > 0:
                avg_ret_proxy = float(real) / max(1.0, float(total_equity)) / trades
                adaptive_edge_cfg["recent_avg_return"] = avg_ret_proxy
                # Win rate is hard to compute cheaply from daily totals; a
                # signed-avg proxy of >0 implies winners > losers. Use a
                # simple binary mapping: any net-positive day defaults to
                # 60% win-rate; negative day to 40%. Phase 4 will replace.
                adaptive_edge_cfg["recent_win_rate"] = 0.6 if real > 0 else 0.4
        except Exception as exc:  # noqa: BLE001
            self._swallow("adaptive_edge_outcome_inputs", exc)
        # Per-bucket / per-symbol net-of-cost evidence governor. Refresh the
        # rolling attribution from the DB on a cadence (cheap replay of the
        # trailing window's fills). A persistently net-negative bucket
        # (trim/recycle/rotation) or symbol (ETH/XRP) then gets a steeply
        # widened edge bar in the coordinator — near-zero turnover while it
        # bleeds, automatic full recovery once it proves net-positive.
        try:
            _attrib_every_n = max(1, int(os.getenv("EDGE_ATTRIB_REFRESH_EVERY_N", "5")))
        except (TypeError, ValueError):
            _attrib_every_n = 5
        if self._edge_attrib_cache is None or (self.iterations % _attrib_every_n == 0):
            try:
                from system.edge_attribution import compute_edge_attribution
                try:
                    _attrib_window = float(os.getenv("EDGE_ATTRIB_WINDOW_DAYS", "4") or 4)
                except (TypeError, ValueError):
                    _attrib_window = 4.0
                async with session_factory() as _attrib_sess:
                    self._edge_attrib_cache = await compute_edge_attribution(
                        _attrib_sess, window_days=_attrib_window
                    )
                if self.iterations % 5 == 0 and isinstance(self._edge_attrib_cache, dict):
                    _negb = {
                        k: round(float(v.get("net", 0.0)), 1)
                        for k, v in (self._edge_attrib_cache.get("buckets") or {}).items()
                        if float(v.get("net", 0.0)) < 0.0
                    }
                    if _negb:
                        logger.info(
                            "edge_attribution | net-negative buckets (throttled): {}",
                            _negb,
                        )
            except Exception as exc:  # noqa: BLE001
                self._swallow("edge_attribution_refresh", exc)
        # Merge onto a shallow copy of the YAML cfg so we don't mutate the
        # underlying loaded dict (other code paths read it).
        coord_cfg = dict(self._global_edge_cfg or {})
        if isinstance(self._edge_attrib_cache, dict) and self._edge_attrib_cache:
            coord_cfg["edge_attribution"] = self._edge_attrib_cache
        if adaptive_edge_cfg:
            coord_cfg["adaptive_edge"] = adaptive_edge_cfg
            if self.iterations % 5 == 0:
                logger.info(
                    "adaptive_edge | cost_bps={} avg_ret={} win_rate={}",
                    adaptive_edge_cfg.get("cross_venue_cost_bps"),
                    adaptive_edge_cfg.get("recent_avg_return"),
                    adaptive_edge_cfg.get("recent_win_rate"),
                )
        coord = GlobalEdgeCoordinator(coord_cfg, logger=logger)
        repl_ctx = await load_replacement_context_from_bus(bus)
        mode_for_coord = mode_raw if mode_raw in ("hunter", "trader", "defender") else "trader"
        if tradable <= 0:
            try:
                cancelled = await self.execution_engine.cancel_working_orders(
                    session_factory=session_factory,
                    reason="capital_allocation_zero",
                )
                if cancelled:
                    logger.info(
                        "global_edge | zero allocation cancelled {} working order(s)",
                        cancelled,
                    )
            except Exception as exc:  # noqa: BLE001
                logger.warning("global_edge | zero allocation cancel working orders failed | {}", exc)
            actions = coord.propose_flatten_actions(
                held,
                active_mode=mode_for_coord,
            )
            actions = sorted(
                actions,
                key=lambda a: (
                    str((a.metadata or {}).get("asset_class", "")).strip().lower() != "crypto",
                    str((a.metadata or {}).get("broker", "")).strip().lower()
                    not in NO_NATIVE_PAPER_POSITION_BROKERS,
                    -abs(Decimal(str(getattr(a, "capital", "0") or "0"))),
                ),
            )
        else:
            # Adaptive sizing path (USE_ADAPTIVE_SIZING=1) — coordinator drives
            # sizing from a tradable-derived gross target + softmax over
            # priority_score, so a single dominant opportunity in Hunter mode
            # can absorb ~100% of the deployable capital. Falls back to legacy
            # per-mode fixed action count + notional fraction when off.
            adaptive_on = adaptive_runtime_on
            adaptive_kwargs: dict[str, Any] = {}
            if self.iterations <= 1:
                logger.info(
                    "trading_loop | adaptive_sizing flag={} env={!r} tradable={} mode={}",
                    adaptive_on,
                    os.environ.get("USE_ADAPTIVE_SIZING"),
                    tradable,
                    mode_for_coord,
                )
            if adaptive_on and tradable > 0:
                adaptive_budget_active = True
                # Phase 5: mode-keyed adaptive block collapsed to scalars
                # at top level of ``adaptive``. Hunter values are canonical.
                # Legacy dict shape (``adaptive.mode.<mode>``) is still
                # tolerated so older YAML configs don't break.
                _adaptive_cfg = self._global_edge_cfg.get("adaptive") or {}
                _legacy_mode_block = _adaptive_cfg.get("mode")
                if isinstance(_legacy_mode_block, dict):
                    _mode_raw_cfg = (
                        _legacy_mode_block.get(mode_for_coord)
                        or _legacy_mode_block.get("trader")
                        or {}
                    )
                    _gf_src = _mode_raw_cfg.get("gross_fraction", _adaptive_cfg.get("gross_fraction", "1.0"))
                    _ce_src = _mode_raw_cfg.get("concentration", _adaptive_cfg.get("concentration", "1.0"))
                else:
                    _gf_src = _adaptive_cfg.get("gross_fraction", "1.0")
                    _ce_src = _adaptive_cfg.get("concentration", "1.0")
                try:
                    _gross_fraction = Decimal(str(_gf_src))
                except Exception:  # noqa: BLE001
                    _gross_fraction = Decimal("1.0")
                try:
                    _concentration = Decimal(str(_ce_src))
                except Exception:  # noqa: BLE001
                    _concentration = Decimal("1.0")
                # Sizing in CASH-DEPLOYED space, not notional. The slider
                # represents the share of NAV the operator is willing to
                # actually tie up; forex notionals use a bounded margin factor
                # gross without consuming much cash, so we measure budgets
                # in cash and convert to notional per asset class downstream.
                cash_target = tradable * _gross_fraction
                from portfolio.global_edge_coordinator import (
                    cash_factor_for_asset_class as _cf,
                )
                _ge_cf_overrides = (
                    self._global_edge_cfg.get("cash_factors") or None
                )
                held_cash_used = sum(
                    (
                        h.notional
                        * _cf(
                            str((h.metadata or {}).get("asset_class") or ""),
                            _ge_cf_overrides,
                            symbol=h.symbol,
                        )
                        for h in held
                    ),
                    Decimal("0"),
                )
                remaining_cash = cash_target - held_cash_used
                # Stash the absolute cash target so a downstream shed-to-target
                # branch (slider down) can read it without recomputing.
                self._last_adaptive_cash_target = cash_target
                self._last_adaptive_held_cash_used = held_cash_used
                try:
                    _build_tol = Decimal(str((self._global_edge_cfg.get("adaptive") or {}).get("target_tolerance_pct", "0.0025")))
                except Exception:  # noqa: BLE001
                    _build_tol = Decimal("0.0025")
                build_threshold = cash_target * max(Decimal("0"), _build_tol)
                if remaining_cash > build_threshold:
                    adaptive_kwargs["gross_target_capital"] = remaining_cash
                    adaptive_kwargs["concentration_exponent"] = _concentration
                    single_name_cfg = risk_cfg.get("single_name_notional") or {}
                    single_name_cap = None
                    if bool(single_name_cfg.get("enabled", True)):
                        try:
                            single_name_cap = Decimal(str(single_name_cfg.get("max_pct_nav", "0.05")))
                        except Exception:  # noqa: BLE001
                            single_name_cap = Decimal("0.05")
                    if single_name_cap is not None and single_name_cap > 0:
                        # The single-name cap is unconditional in RiskEngine.
                        # Tell the coordinator up front so at-cap symbols do
                        # not consume action slots only to be clamped to dust.
                        adaptive_kwargs["max_position_notional"] = total_equity * single_name_cap
                    elif enforce_static_exposure_caps:
                        # Legacy compatibility for installs that disable the
                        # single-name rail but still opt into static caps.
                        adaptive_kwargs["max_position_notional"] = total_equity * max_pos_pct
            else:
                adaptive_budget_active = False
            # Shed-to-target: when held cash exceeds the slider's new cash
            # target by > 5%, immediately emit reduce-only trims so the book
            # converges to the operator's target without waiting for natural
            # displacement. This is the slider-down counterpart to the
            # adaptive build-up path.
            shed_actions: list = []
            if (
                adaptive_on
                and tradable > 0
                and getattr(self, "_last_adaptive_cash_target", None) is not None
            ):
                _ct = self._last_adaptive_cash_target
                _hc = self._last_adaptive_held_cash_used
                _adapt_cfg = (self._global_edge_cfg.get("adaptive") or {})
                try:
                    _tol = Decimal(str(_adapt_cfg.get("target_tolerance_pct", "0.0025")))
                except Exception:  # noqa: BLE001
                    _tol = Decimal("0.0025")
                if _hc > _ct * (Decimal("1") + max(Decimal("0"), _tol)):
                    # Soft-shed: cap this iteration's forced reduction at
                    # ``soft_shed_step_pct_of_nav`` × NAV. Pass an inflated
                    # target to ``propose_shed_actions`` so it only trims
                    # enough to hit the soft-step, not the full target.
                    # Subsequent iterations carry the rest until convergence.
                    try:
                        _step_pct = Decimal(str(_adapt_cfg.get("soft_shed_step_pct_of_nav", "0.10")))
                    except Exception:  # noqa: BLE001
                        _step_pct = Decimal("0.10")
                    _step_pct = max(Decimal("0"), _step_pct)
                    soft_step_cash = total_equity * _step_pct
                    full_excess = _hc - _ct
                    if soft_step_cash > 0 and soft_step_cash < full_excess:
                        soft_target = _hc - soft_step_cash
                        logger.info(
                            "trading_loop | soft-shed | held_cash={} target={} excess={} step={} (cap {}% of NAV)",
                            _hc, _ct, full_excess, soft_step_cash,
                            float(_step_pct) * 100,
                        )
                    else:
                        soft_target = _ct
                    shed_actions = coord.propose_shed_actions(
                        held,
                        cash_target_absolute=soft_target,
                        active_mode=mode_for_coord,
                        replacement_context=repl_ctx,
                    )
                    if shed_actions:
                        logger.info(
                            "trading_loop | adaptive shed-to-target | held_cash={} > target={} | actions={}",
                            _hc,
                            soft_target,
                            len(shed_actions),
                        )
            if adaptive_budget_active and not adaptive_kwargs:
                # Adaptive mode is already at/above the slider cash target.
                # Keep Hunter alive by rotating weak held positions into
                # stronger fresh opportunities when expected edge beats
                # estimated round-trip costs.
                actions = coord.propose_rotation_actions(
                    held,
                    new_opps_dedup,
                    active_mode=mode_for_coord,
                    replacement_context=repl_ctx,
                )
                # Capital-recycling safety net (audit #3): rotation only fires
                # when a fresh opp beats a holding by min_edge_advantage+fees.
                # When nothing clears that, the book would stay 100% deployed
                # and idle forever. Independently bank take-profit winners and
                # cull dead-edge positions; the freed cash is redeployed by
                # the build-up path next iteration. Prepended so closes settle
                # before any same-tick rotation opens.
                try:
                    recycle_actions = coord.propose_capital_recycle_actions(
                        held,
                        active_mode=mode_for_coord,
                        replacement_context=repl_ctx,
                    )
                except Exception as exc:  # noqa: BLE001
                    recycle_actions = []
                    logger.debug("trading_loop | capital_recycle failed: {}", exc)
                if recycle_actions:
                    logger.info(
                        "trading_loop | capital_recycle | freeing cash via {} reduce-only action(s) | sample={}",
                        len(recycle_actions),
                        [
                            (a.symbol, (a.metadata or {}).get("capital_recycle_reason"))
                            for a in recycle_actions[:5]
                        ],
                    )
                    actions = list(recycle_actions) + list(actions)
            else:
                actions = coord.propose_actions(
                    held,
                    new_opps_dedup,
                    active_mode=mode_for_coord,
                    replacement_context=repl_ctx,
                    **adaptive_kwargs,
                )
            if shed_actions:
                # Run shed first (close oversized positions), then any new
                # opens. Together they bring cash to target.
                actions = list(shed_actions) + list(actions)
            try:
                existing_reduce_keys = {
                    (
                        str(getattr(a, "symbol", "") or "").strip().upper(),
                        str((getattr(a, "metadata", None) or {}).get("broker") or "").strip().lower(),
                    )
                    for a in actions
                    if str(getattr(a, "kind", "") or "") == "trim_symbol"
                }
                session_exit_actions = [
                    a
                    for a in coord.propose_session_exit_actions(
                        held,
                        active_mode=mode_for_coord,
                        now=datetime.now(timezone.utc),
                    )
                    if (
                        str(getattr(a, "symbol", "") or "").strip().upper(),
                        str((getattr(a, "metadata", None) or {}).get("broker") or "").strip().lower(),
                    )
                    not in existing_reduce_keys
                ]
            except Exception as exc:  # noqa: BLE001
                session_exit_actions = []
                logger.debug("trading_loop | session_exit_policy failed: {}", exc)
            if session_exit_actions:
                logger.info(
                    "trading_loop | session_exit_policy | {} pre-close reduce action(s) | sample={}",
                    len(session_exit_actions),
                    [
                        (
                            a.symbol,
                            (a.metadata or {}).get("session_exit_action"),
                            (a.metadata or {}).get("session_exit_reason"),
                        )
                        for a in session_exit_actions[:5]
                    ],
                )
                actions = list(session_exit_actions) + list(actions)
            try:
                if repl_ctx is not None and actions:
                    ts = datetime.now(timezone.utc)

                    def _ctx_sym(raw: str) -> str:
                        s = str(raw or "").strip().upper()
                        for suf in ("=X", "=F"):
                            if s.endswith(suf):
                                return s[: -len(suf)]
                        if s.endswith("-USD") and len(s) > 4:
                            return s[:-4]
                        return s

                    for act in actions:
                        if str(getattr(act, "kind", "") or "") not in {"open_strategy", "trim_symbol"}:
                            continue
                        sym_key = _ctx_sym(str(getattr(act, "symbol", "") or ""))
                        if not sym_key:
                            continue
                        repl_ctx.last_event_at_by_symbol[sym_key] = ts
                        # Record reduce-only CULLs (capital-recycle dead-edge
                        # close / adaptive-shed) separately so the build-up
                        # path's re-entry debounce can block re-opening a
                        # just-culled name without also blocking legitimate
                        # top-ups of normally-opened positions.
                        _am = getattr(act, "metadata", None) or {}
                        _sp = str(_am.get("sizing_path", "") or "").lower()
                        _strat = str(getattr(act, "strategy_name", "") or "").lower()
                        is_cull = (
                            bool(_am.get("capital_recycle_reason"))
                            or _sp in {"capital_recycle", "adaptive_shed_to_target"}
                            or _strat in {"capital_recycle", "adaptive_shed"}
                        )
                        if is_cull:
                            repl_ctx.last_cull_at_by_symbol[sym_key] = ts
                await save_replacement_context_to_bus(bus, repl_ctx)
            except Exception as exc:  # noqa: BLE001
                self._swallow("save_replacement_context", exc)
        actions, preflight_rows = await self._preflight_coordinator_actions(
            actions,
            session_factory=session_factory,
            portfolio_dict=portfolio_dict,
            portfolio_value=tradable,
            ai_result=ai_result,
        )
        if buf is not None and preflight_rows:
            buf.extend(preflight_rows)
        if buf is not None and not actions and new_opps:
            try:
                buf.append(
                    strategy_candidate_row(
                        symbol="GLOBAL_EDGE",
                        strategy="allocator",
                        status="scanned_no_action",
                        reason="insufficient_edge_or_budget",
                        metadata={
                            "opportunities_seen": str(len(new_opps)),
                            "held_count": str(len(held)),
                            "capital_pct": str(self.capital_pct),
                            "adaptive_sizing": str(adaptive_runtime_on),
                        },
                        loop_iteration=self.iterations,
                    )
                )
            except Exception:  # noqa: BLE001
                pass
        log_arb_event("rank", ranked=len(actions), opportunities=len(new_opps), held=len(held))

        dashboard_snapshot_written = False
        try:
            ps_ge = portfolio_dict_to_runtime_state(
                portfolio_dict,
                mode=mode_raw,
                capital_pct=float(self.capital_pct),
            )
            ge_regime = None
            try:
                alloc_cfg_ge = load_allocation()
                async with session_factory() as session:
                    ge_regime = await compute_regime_state_async(
                        portfolio_state=ps_ge,
                        allocation_cfg=alloc_cfg_ge,
                        session=session,
                        universe_symbols=list(symbols),
                        timeframe=self.timeframe,
                    )
                    ge_regime.metadata = dict(ge_regime.metadata or {})
                    ge_regime.metadata["demand_score"] = round(float(demand_score), 6)
                    ge_regime.metadata["demand_trend"] = demand_trend
                    ge_regime.metadata["demand_confidence"] = round(float(demand_confidence), 6)
                    ge_regime.metadata["market_volatility"] = float(
                        demand_components.get("market_volatility", 0.0)
                    )
                    ge_regime.metadata["cross_asset_coverage"] = float(
                        demand_components.get("cross_asset_coverage", 0.0)
                    )
                    if demand_alert is not None:
                        ge_regime.metadata["demand_alert"] = dict(demand_alert)
                    if demand_alert_history:
                        ge_regime.metadata["demand_alert_history"] = list(demand_alert_history[-8:])
            except Exception as regime_exc:  # noqa: BLE001
                logger.debug("global_edge | regime shadow unavailable | {}", regime_exc)
            await publish_dashboard_snapshot_global_edge(
                bus,
                loop_iteration=self.iterations,
                accumulator=self.sig_engine.accumulator if self.sig_engine else None,
                held=held,
                strategy_opportunities=new_opps,
                coordinator_actions=actions,
                portfolio_state=ps_ge,
                regime=ge_regime,
                demand={
                    "score": round(float(demand_score), 6),
                    "trend": demand_trend,
                    "confidence": round(float(demand_confidence), 6),
                    "components": {
                        k: round(float(v), 6) for k, v in (demand_components or {}).items()
                    },
                    "alert": dict(demand_alert) if demand_alert is not None else None,
                    "alert_history": list(demand_alert_history[-8:]),
                },
                strategy_pnl_recent=strategy_pnl_recent,
            )
            dashboard_snapshot_written = True
        except Exception as pub_exc:  # noqa: BLE001
            logger.warning("dashboard_publish | global_edge | {}", pub_exc)

        # Boot warmup churn-guard: on the first cycle(s) after ANY restart,
        # drop position-reducing actions so a fresh process cannot cull and
        # re-buy on un-rebuilt state (the close→reopen restart bleed). Applied
        # AFTER shed/recycle prepends so those are gated too; opens/arbitrage
        # pass; risk/stop-loss exits are a separate path and never reach here.
        actions = self._suppress_reducing_actions_during_warmup(actions)

        executed = 0
        planner_cfg = {
            "size_fractions": self._global_edge_cfg.get("size_fractions", [0.25, 0.5, 0.75, 1.0]),
            "max_slippage_bps": self._global_edge_cfg.get("max_slippage_bps", "25"),
            "min_simulated_edge_bps": self._global_edge_cfg.get("min_simulated_edge_bps", "0"),
        }
        planner = ExecutionPlanner(OrderBookAnalyzer(), planner_cfg)

        # Audit #10: bound the blast radius of a slow/hung broker. Fully
        # parallelising this loop is unsafe — every action runs through the
        # risk engine + execution + portfolio accounting, and concurrent
        # actions would race capital and over-deploy. Instead we cap the
        # wall-clock the action batch may consume: in-flight actions are
        # never cancelled (no partial DB/exec state), we simply stop
        # STARTING new ones once the budget is spent; the rest are
        # reconsidered next iteration. This stops one stuck broker call on
        # action #1 from eating the whole loop interval and starving the
        # other 19 + every downstream opportunity.
        try:
            _act_budget = float(os.getenv("ACTION_BATCH_BUDGET_SEC", "45") or 45)
        except (TypeError, ValueError):
            _act_budget = 45.0
        _act_budget = max(5.0, _act_budget)
        _act_started = time.monotonic()
        try:
            _ob_timeout = float(os.getenv("ACTION_ORDERBOOK_TIMEOUT_SEC", "8") or 8)
        except (TypeError, ValueError):
            _ob_timeout = 8.0
        _act_deferred = 0

        for _act_idx, action in enumerate(actions):
            if _act_idx > 0 and (time.monotonic() - _act_started) > _act_budget:
                _act_deferred = len(actions) - _act_idx
                logger.warning(
                    "trading_loop | action batch budget {}s exhausted after {} action(s) | "
                    "deferring {} to next iteration",
                    _act_budget, _act_idx, _act_deferred,
                )
                break
            sig = process_coordinator_action(
                action,
                self.sig_engine,
                portfolio_value=tradable,
                news_score=_news_score_for_symbol(ai_result, action.symbol),
            )
            if sig is None:
                if sc_log_buffer is not None:
                    sc_log_buffer.append(
                        strategy_candidate_row(
                            symbol=str(action.symbol),
                            strategy=str(action.strategy_name),
                            status="execution_incomplete",
                            reason="signal_engine_returned_none",
                            loop_iteration=self.iterations,
                            metadata={"kind": str(action.kind)},
                        )
                    )
                log_arb_event("reject", reason="signal_null", symbol=action.symbol)
                continue
            if action.strategy_name == "cross_exchange_arbitrage" and self._broker_manager:
                md = sig.metadata or {}
                buy_v = str(md.get("buy_venue", "")).strip().lower()
                sell_v = str(md.get("sell_venue", "")).strip().lower()
                ba = self._broker_manager.adapters.get(buy_v)
                sa = self._broker_manager.adapters.get(sell_v)
                if ba and sa:
                    try:
                        # Timeout the order-book reads — a hung venue here
                        # would otherwise block the whole action batch. Pure
                        # reads, safe to cancel (audit #10).
                        ob_b, ob_s = await asyncio.wait_for(
                            asyncio.gather(
                                ba.get_order_book(sig.symbol, depth=25),
                                sa.get_order_book(sig.symbol, depth=25),
                            ),
                            timeout=_ob_timeout,
                        )
                        plan = planner.plan_trade(ob_b, ob_s, min(sig.suggested_quantity * (sig.suggested_price or Decimal("1")), tradable * Decimal("0.15")))
                        if plan is None:
                            log_arb_event("reject", reason="execution_planner", symbol=sig.symbol)
                            continue
                        sig.suggested_quantity = plan["quantity"]
                        sig.metadata = dict(sig.metadata or {})
                        sig.metadata["buy_limit_from_ask"] = str(plan["buy_price"])
                        sig.metadata["sell_limit_from_bid"] = str(plan["sell_price"])
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("global_edge | planner failed | {}", exc)
                        log_arb_event("reject", reason="planner_error", symbol=sig.symbol)
                        continue

            block_reason, block_meta = await self._preflight_built_signal(
                sig,
                action,
                portfolio_dict=portfolio_dict,
                session_factory=session_factory,
            )
            if block_reason:
                if sc_log_buffer is not None:
                    sc_log_buffer.append(
                        strategy_candidate_row(
                            symbol=str(getattr(sig, "symbol", "") or ""),
                            strategy=str(getattr(sig, "strategy", "") or ""),
                            side=str(getattr(sig, "side", "") or ""),
                            confidence=float(getattr(sig, "confidence", 0) or 0),
                            status="filtered_preflight_viability",
                            reason=block_reason,
                            loop_iteration=self.iterations,
                            metadata={
                                "kind": str(getattr(action, "kind", "") or ""),
                                "priority_score": str(getattr(action, "priority_score", "") or ""),
                                **block_meta,
                            },
                        )
                    )
                log_arb_event("reject", reason=block_reason, symbol=getattr(sig, "symbol", ""))
                continue

            if sc_log_buffer is not None:
                try:
                    sc_log_buffer.append(
                        strategy_candidate_row(
                            symbol=str(action.symbol),
                            strategy=str(action.strategy_name),
                            status="selected_for_allocation",
                            reason="global_edge_coordinator",
                            metadata={"priority_score": str(action.priority_score), "kind": str(action.kind)},
                            loop_iteration=self.iterations,
                        )
                    )
                except Exception as exc:  # noqa: BLE001
                    self._swallow("sc_buffer_selected_for_allocation", exc)

            ok = await self._process_signal_global(
                sig,
                session_factory,
                portfolio_dict,
                total_equity,
                tradable,
                sc_log_buffer=sc_log_buffer,
            )
            if ok:
                executed += 1
                log_arb_event("execute", symbol=sig.symbol, strategy=sig.strategy, side=sig.side)
        return executed, dashboard_snapshot_written

    async def _preflight_built_signal(
        self,
        signal: Any,
        action: Any,
        *,
        portfolio_dict: dict[str, Any],
        session_factory: Any | None = None,
    ) -> tuple[str | None, dict[str, Any]]:
        """Preflight the exact signal instance that is about to be executed."""
        md = getattr(signal, "metadata", None)
        md = md if isinstance(md, dict) else {}
        is_reduce = bool(
            md.get("reduce_only")
            or md.get("close_only")
            or str(md.get("coordinator_kind", "")).lower() == "trim_symbol"
        )
        preferred_broker = str(md.get("broker") or getattr(signal, "broker", "") or "").strip().lower()
        if is_reduce and preferred_broker and (preferred_broker in self.available_brokers or self.paper_mode):
            routed = preferred_broker
        elif self._can_route_broker(preferred_broker, str(signal.asset_class)) and "broker" in md:
            routed = preferred_broker
        elif self.router is not None:
            routed = self.router.route(signal.asset_class, signal.symbol, metadata=md)
        else:
            routed = preferred_broker
        if routed is None:
            return "no_route", {"broker": ""}
        signal.broker = routed
        native = broker_symbol_for(signal.symbol, routed)
        if native and native != signal.symbol:
            signal.metadata = dict(md)
            signal.metadata.setdefault("pipeline_symbol", signal.symbol)
            signal.symbol = native
            md = signal.metadata

        if self.risk_engine is not None:
            try:
                pre = self.risk_engine.preflight_capacity(signal, portfolio_dict)
            except Exception as exc:  # noqa: BLE001
                logger.debug("global_edge | built-signal risk preflight failed open | {}", exc)
                pre = None
            if pre is not None and not pre.ok:
                return str(pre.reason), {
                    "checks_failed": list(pre.checks_failed or []),
                    "effective_notional": str(pre.effective_notional),
                    "broker": str(routed),
                }
            if pre is not None:
                try:
                    effective_qty = Decimal(str(pre.effective_quantity))
                    current_qty = Decimal(str(getattr(signal, "suggested_quantity", "0") or "0"))
                except Exception:  # noqa: BLE001
                    effective_qty = current_qty = Decimal("0")
                if effective_qty > 0 and current_qty > 0 and effective_qty < current_qty:
                    probe = deepcopy(signal)
                    probe.suggested_quantity = effective_qty
                    probe_md = dict(getattr(probe, "metadata", None) or {})
                    probe_md["preflight_capacity_effective_quantity"] = str(effective_qty)
                    probe_md["preflight_capacity_effective_notional"] = str(pre.effective_notional)
                    probe.metadata = probe_md
                    signal_for_execution_preflight = probe
                else:
                    signal_for_execution_preflight = signal
            else:
                signal_for_execution_preflight = signal

        cost_block_reason, cost_meta = self._preflight_wave9_cost(signal_for_execution_preflight)
        if cost_block_reason:
            return cost_block_reason, {"broker": str(routed), **cost_meta}

        exec_block_reason, exec_meta = await self._preflight_execution_limits(
            signal_for_execution_preflight,
            session_factory=session_factory,
        )
        if exec_block_reason:
            if exec_block_reason == "execution_precheck_rejected":
                original_symbol = str(getattr(signal, "symbol", "") or "")
                original_md = dict(md)
                for alt in self._alternate_brokers_for_signal(signal, routed, metadata=original_md):
                    alt_sig = deepcopy(signal)
                    alt_sig.symbol = original_symbol
                    alt_sig.metadata = dict(original_md)
                    alt_sig.broker = alt
                    native_alt = broker_symbol_for(alt_sig.symbol, alt)
                    if native_alt and native_alt != alt_sig.symbol:
                        alt_sig.metadata.setdefault("pipeline_symbol", alt_sig.symbol)
                        alt_sig.symbol = native_alt
                    alt_for_execution = alt_sig
                    if self.risk_engine is not None:
                        try:
                            alt_pre = self.risk_engine.preflight_capacity(alt_sig, portfolio_dict)
                        except Exception as exc:  # noqa: BLE001
                            logger.debug("global_edge | fallback risk preflight failed open | {}", exc)
                            alt_pre = None
                        if alt_pre is not None and not alt_pre.ok:
                            continue
                        if alt_pre is not None:
                            try:
                                alt_effective_qty = Decimal(str(alt_pre.effective_quantity))
                                alt_current_qty = Decimal(str(getattr(alt_sig, "suggested_quantity", "0") or "0"))
                            except Exception:  # noqa: BLE001
                                alt_effective_qty = alt_current_qty = Decimal("0")
                            if alt_effective_qty > 0 and alt_current_qty > 0 and alt_effective_qty < alt_current_qty:
                                alt_for_execution = deepcopy(alt_sig)
                                alt_for_execution.suggested_quantity = alt_effective_qty
                                alt_md = dict(getattr(alt_for_execution, "metadata", None) or {})
                                alt_md["preflight_capacity_effective_quantity"] = str(alt_effective_qty)
                                alt_md["preflight_capacity_effective_notional"] = str(alt_pre.effective_notional)
                                alt_for_execution.metadata = alt_md
                    alt_cost_reason, _alt_cost_meta = self._preflight_wave9_cost(alt_for_execution)
                    if alt_cost_reason:
                        continue
                    alt_exec_reason, alt_exec_meta = await self._preflight_execution_limits(
                        alt_for_execution,
                        session_factory=session_factory,
                    )
                    if alt_exec_reason is None:
                        signal.broker = alt
                        signal.metadata = dict(original_md)
                        signal.metadata["broker"] = alt
                        signal.metadata["execution_preflight_fallback_from"] = str(routed)
                        if getattr(action, "metadata", None) is not None:
                            action.metadata["broker"] = alt
                            action.metadata["execution_preflight_fallback_from"] = str(routed)
                        return None, {
                            "broker": alt,
                            "execution_preflight_fallback_from": str(routed),
                            **alt_exec_meta,
                        }
            return exec_block_reason, {"broker": str(routed), **exec_meta}

        return None, {"broker": str(routed)}

    def _can_route_broker(self, broker: str, asset_class: str) -> bool:
        b = str(broker or "").strip().lower()
        ac = str(asset_class or "").strip().lower()
        if not b or b not in set(self.available_brokers or []):
            return False
        if ac not in BROKER_ASSET_MAP.get(b, set()):
            return False
        try:
            return bool(self.router is None or self.router.permissions.check_permission(b, ac))
        except Exception:  # noqa: BLE001
            return True

    def _alternate_brokers_for_signal(
        self,
        signal: Any,
        routed: str | None,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> list[str]:
        md = metadata if isinstance(metadata, dict) else {}
        asset_class = str(getattr(signal, "asset_class", "") or "").strip().lower()
        current = str(routed or "").strip().lower()
        out: list[str] = []
        for broker in self.available_brokers or []:
            b = str(broker or "").strip().lower()
            if not b or b == current or not self._can_route_broker(b, asset_class):
                continue
            if asset_class == "crypto" and b == "alpaca" and not md.get("allow_alpaca_crypto"):
                continue
            out.append(b)
        if asset_class in {"equity", "etf"}:
            out.sort(key=lambda b: (0 if b == "ibkr" else 1, b))
        elif asset_class == "crypto":
            out.sort(key=lambda b: ({"kraken": 0, "binance": 1, "bybit": 2, "ibkr": 3}.get(b, 9), b))
        return out

    async def _preflight_coordinator_actions(
        self,
        actions: list[Any],
        *,
        session_factory: Any,
        portfolio_dict: dict[str, Any],
        portfolio_value: Decimal,
        ai_result: Any | None = None,
    ) -> tuple[list[Any], list[dict[str, Any]]]:
        """Filter allocator actions using final-stage risk/cost gates.

        This deliberately calls ``RiskEngine.preflight_capacity`` and Wave 9's
        runtime cost gate rather than duplicating their rules in the allocator.
        The final risk/execution pass still runs later; this pass only stops
        known dead-on-arrival opens from being logged as selected work.
        """
        if not actions or self.sig_engine is None:
            return actions, []

        viable: list[Any] = []
        rows: list[dict[str, Any]] = []
        for action in actions:
            selected_action = action
            try:
                sig = process_coordinator_action(
                    action,
                    self.sig_engine,
                    portfolio_value=portfolio_value,
                    news_score=_news_score_for_symbol(ai_result, str(getattr(action, "symbol", "") or "")),
                )
            except Exception as exc:  # noqa: BLE001
                sig = None
                logger.debug("global_edge | preflight signal build failed | {}", exc)
            if sig is None:
                rows.append(
                    strategy_candidate_row(
                        symbol=str(getattr(action, "symbol", "") or ""),
                        strategy=str(getattr(action, "strategy_name", "") or ""),
                        status="filtered_preflight_viability",
                        reason="signal_engine_returned_none",
                        loop_iteration=self.iterations,
                        metadata={"kind": str(getattr(action, "kind", "") or "")},
                    )
                )
                continue

            md = getattr(sig, "metadata", None)
            md = md if isinstance(md, dict) else {}
            is_reduce = bool(
                md.get("reduce_only")
                or md.get("close_only")
                or str(md.get("coordinator_kind", "")).lower() == "trim_symbol"
            )
            preferred_broker = str(md.get("broker") or getattr(sig, "broker", "") or "").strip().lower()
            if is_reduce and preferred_broker and (preferred_broker in self.available_brokers or self.paper_mode):
                routed = preferred_broker
            elif self._can_route_broker(preferred_broker, str(sig.asset_class)) and "broker" in md:
                routed = preferred_broker
            elif self.router is not None:
                routed = self.router.route(sig.asset_class, sig.symbol, metadata=md)
            else:
                routed = preferred_broker
            if routed is None:
                rows.append(
                    strategy_candidate_row(
                        symbol=str(getattr(sig, "symbol", "") or ""),
                        strategy=str(getattr(sig, "strategy", "") or ""),
                        side=str(getattr(sig, "side", "") or ""),
                        confidence=float(getattr(sig, "confidence", 0) or 0),
                        status="filtered_preflight_viability",
                        reason="no_route",
                        loop_iteration=self.iterations,
                        metadata={"kind": str(getattr(action, "kind", "") or "")},
                    )
                )
                continue
            sig.broker = routed
            native = broker_symbol_for(sig.symbol, routed)
            if native and native != sig.symbol:
                sig.metadata = dict(md)
                sig.metadata.setdefault("pipeline_symbol", sig.symbol)
                sig.symbol = native
                md = sig.metadata

            if self.risk_engine is not None:
                try:
                    pre = self.risk_engine.preflight_capacity(sig, portfolio_dict)
                except Exception as exc:  # noqa: BLE001
                    logger.debug("global_edge | risk preflight failed open | {}", exc)
                    pre = None
                if pre is not None and not pre.ok:
                    rows.append(
                        strategy_candidate_row(
                            symbol=str(getattr(sig, "symbol", "") or ""),
                            strategy=str(getattr(sig, "strategy", "") or ""),
                            side=str(getattr(sig, "side", "") or ""),
                            confidence=float(getattr(sig, "confidence", 0) or 0),
                            status="filtered_preflight_viability",
                            reason=str(pre.reason),
                            loop_iteration=self.iterations,
                            metadata={
                                "kind": str(getattr(action, "kind", "") or ""),
                                "checks_failed": list(pre.checks_failed or []),
                                "effective_notional": str(pre.effective_notional),
                                "broker": str(routed),
                            },
                        )
                    )
                    continue
                if pre is not None:
                    try:
                        effective_qty = Decimal(str(pre.effective_quantity))
                        current_qty = Decimal(str(getattr(sig, "suggested_quantity", "0") or "0"))
                    except Exception:  # noqa: BLE001
                        effective_qty = current_qty = Decimal("0")
                    if effective_qty > 0 and current_qty > 0 and effective_qty < current_qty:
                        sig_probe = deepcopy(sig)
                        sig_probe.suggested_quantity = effective_qty
                        sig_probe_md = dict(getattr(sig_probe, "metadata", None) or {})
                        sig_probe_md["preflight_capacity_effective_quantity"] = str(effective_qty)
                        sig_probe_md["preflight_capacity_effective_notional"] = str(pre.effective_notional)
                        sig_probe.metadata = sig_probe_md
                        sig_for_execution_preflight = sig_probe
                        selected_action = self._coordinator_action_with_effective_capacity(
                            action,
                            effective_notional=pre.effective_notional,
                            effective_quantity=effective_qty,
                        )
                    else:
                        sig_for_execution_preflight = sig
                else:
                    sig_for_execution_preflight = sig
            else:
                sig_for_execution_preflight = sig

            cost_block_reason, cost_meta = self._preflight_wave9_cost(sig_for_execution_preflight)
            if cost_block_reason:
                rows.append(
                    strategy_candidate_row(
                        symbol=str(getattr(sig, "symbol", "") or ""),
                        strategy=str(getattr(sig, "strategy", "") or ""),
                        side=str(getattr(sig, "side", "") or ""),
                        confidence=float(getattr(sig, "confidence", 0) or 0),
                        status="filtered_preflight_viability",
                        reason=cost_block_reason,
                        loop_iteration=self.iterations,
                        metadata={
                            "kind": str(getattr(action, "kind", "") or ""),
                            "broker": str(routed),
                            **cost_meta,
                        },
                    )
                )
                continue

            exec_block_reason, exec_meta = await self._preflight_execution_limits(
                sig_for_execution_preflight,
                session_factory=session_factory,
            )
            if exec_block_reason:
                fallback_broker = ""
                if exec_block_reason == "execution_precheck_rejected":
                    original_symbol = str(getattr(sig, "symbol", "") or "")
                    original_md = dict(md)
                    for alt in self._alternate_brokers_for_signal(sig, routed, metadata=original_md):
                        alt_sig = deepcopy(sig)
                        alt_sig.symbol = original_symbol
                        alt_sig.metadata = dict(original_md)
                        alt_sig.broker = alt
                        native_alt = broker_symbol_for(alt_sig.symbol, alt)
                        if native_alt and native_alt != alt_sig.symbol:
                            alt_sig.metadata.setdefault("pipeline_symbol", alt_sig.symbol)
                            alt_sig.symbol = native_alt
                        alt_for_execution = alt_sig
                        if self.risk_engine is not None:
                            try:
                                alt_pre = self.risk_engine.preflight_capacity(alt_sig, portfolio_dict)
                            except Exception as exc:  # noqa: BLE001
                                logger.debug("global_edge | coordinator fallback risk preflight failed open | {}", exc)
                                alt_pre = None
                            if alt_pre is not None and not alt_pre.ok:
                                continue
                            if alt_pre is not None:
                                try:
                                    alt_effective_qty = Decimal(str(alt_pre.effective_quantity))
                                    alt_current_qty = Decimal(str(getattr(alt_sig, "suggested_quantity", "0") or "0"))
                                except Exception:  # noqa: BLE001
                                    alt_effective_qty = alt_current_qty = Decimal("0")
                                if alt_effective_qty > 0 and alt_current_qty > 0 and alt_effective_qty < alt_current_qty:
                                    alt_for_execution = deepcopy(alt_sig)
                                    alt_for_execution.suggested_quantity = alt_effective_qty
                                    alt_md = dict(getattr(alt_for_execution, "metadata", None) or {})
                                    alt_md["preflight_capacity_effective_quantity"] = str(alt_effective_qty)
                                    alt_md["preflight_capacity_effective_notional"] = str(alt_pre.effective_notional)
                                    alt_for_execution.metadata = alt_md
                        alt_cost_reason, _alt_cost_meta = self._preflight_wave9_cost(alt_for_execution)
                        if alt_cost_reason:
                            continue
                        alt_exec_reason, _alt_exec_meta = await self._preflight_execution_limits(
                            alt_for_execution,
                            session_factory=session_factory,
                        )
                        if alt_exec_reason is None:
                            selected_meta = dict(getattr(selected_action, "metadata", {}) or {})
                            selected_meta["broker"] = alt
                            selected_meta["execution_preflight_fallback_from"] = str(routed)
                            selected_action = self._coordinator_action_with_metadata(selected_action, selected_meta)
                            fallback_broker = alt
                            break
                if fallback_broker:
                    viable.append(selected_action)
                    continue
                rows.append(
                    strategy_candidate_row(
                        symbol=str(getattr(sig, "symbol", "") or ""),
                        strategy=str(getattr(sig, "strategy", "") or ""),
                        side=str(getattr(sig, "side", "") or ""),
                        confidence=float(getattr(sig, "confidence", 0) or 0),
                        status="filtered_preflight_viability",
                        reason=exec_block_reason,
                        loop_iteration=self.iterations,
                        metadata={
                            "kind": str(getattr(action, "kind", "") or ""),
                            "broker": str(routed),
                            **exec_meta,
                        },
                    )
                )
                continue

            viable.append(selected_action)
        return viable, rows

    def _coordinator_action_with_metadata(self, action: Any, metadata: dict[str, Any]) -> Any:
        """Return an action copy carrying updated metadata when possible."""
        if is_dataclass(action):
            try:
                return dataclass_replace(action, metadata=metadata)
            except Exception:  # noqa: BLE001
                pass
        try:
            action.metadata = metadata
        except Exception:  # noqa: BLE001
            pass
        return action

    def _coordinator_action_with_effective_capacity(
        self,
        action: Any,
        *,
        effective_notional: Decimal,
        effective_quantity: Decimal,
    ) -> Any:
        """Resize a selected coordinator action to risk's executable capacity.

        Risk preflight may clamp an open to remaining single-name / cluster /
        daily-add room. The execution preflight already checks that smaller
        probe; this also carries the smaller notional back into the selected
        action so dashboard rows, sizing metadata, and final processing stay
        aligned with the executable reality.
        """
        if effective_notional <= 0:
            return action
        meta = dict(getattr(action, "metadata", {}) or {})
        original_capital = getattr(action, "capital", None)
        meta.setdefault("preflight_capacity_original_action_capital", str(original_capital))
        meta["preflight_capacity_effective_quantity"] = str(effective_quantity)
        meta["preflight_capacity_effective_notional"] = str(effective_notional)
        meta["target_notional"] = str(effective_notional)
        meta["risk_notional_override"] = str(effective_notional)
        meta["sizing_final_capital_required"] = str(effective_notional)
        meta["sizing_final_action_capital"] = str(effective_notional)
        try:
            cash_factor = Decimal(str(meta.get("sizing_cash_factor", "1") or "1"))
        except Exception:  # noqa: BLE001
            cash_factor = Decimal("1")
        if cash_factor <= 0:
            cash_factor = Decimal("1")
        meta["sizing_cash_used"] = str((effective_notional * cash_factor).quantize(Decimal("0.01")))
        if is_dataclass(action):
            try:
                return dataclass_replace(action, capital=effective_notional, metadata=meta)
            except Exception:  # noqa: BLE001
                pass
        try:
            action.capital = effective_notional
            action.metadata = meta
        except Exception:  # noqa: BLE001
            pass
        return action

    def _preflight_wave9_cost(self, signal: Any) -> tuple[str | None, dict[str, Any]]:
        """Run the same Wave 9 edge/cost gate used by execution, if loaded."""
        md = getattr(signal, "metadata", None)
        md = md if isinstance(md, dict) else {}
        is_reduce = bool(
            md.get("reduce_only")
            or md.get("close_only")
            or str(md.get("coordinator_kind", "")).lower() == "trim_symbol"
        )
        if is_reduce or self.execution_engine is None:
            return None, {}
        wave9_cfg = getattr(self.execution_engine, "_wave9_cfg", None)
        if wave9_cfg is None or not getattr(wave9_cfg, "enabled", False):
            return None, {}
        try:
            from execution.wave9_runtime import pre_flight_cost_gate

            gate = pre_flight_cost_gate(
                config=wave9_cfg,
                broker=str(getattr(signal, "broker", "") or ""),
                symbol=str(getattr(signal, "symbol", "") or ""),
                asset_class=str(getattr(signal, "asset_class", "") or "other"),
                quantity=float(getattr(signal, "suggested_quantity", 0) or 0),
                signal_metadata=md,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("global_edge | wave9 preflight failed open | {}", exc)
            return None, {}
        if not getattr(gate, "used", False) or getattr(gate, "allow", True):
            return None, dict(getattr(gate, "metadata", {}) or {})
        return f"wave9_gate:{getattr(gate, 'reason', 'blocked')}", dict(
            getattr(gate, "metadata", {}) or {}
        )

    async def _preflight_execution_limits(
        self,
        signal: Any,
        *,
        session_factory: Any | None = None,
    ) -> tuple[str | None, dict[str, Any]]:
        """Run execution quantity and microstructure limits before selection."""
        md = getattr(signal, "metadata", None)
        md = md if isinstance(md, dict) else {}
        is_reduce = bool(
            md.get("reduce_only")
            or md.get("close_only")
            or str(md.get("coordinator_kind", "")).lower() == "trim_symbol"
        )
        if is_reduce or self.execution_engine is None:
            return None, {}
        try:
            broker = await self.execution_engine._get_broker(str(getattr(signal, "broker", "") or ""))  # noqa: SLF001
            if broker is None:
                return "broker_unavailable", {"execution_preflight_stage": "broker_resolution"}
            order = self.execution_engine._build_order(signal)  # noqa: SLF001
            pre_qty = order.quantity
            pre_price = order.limit_price
            order = await self.execution_engine._apply_marketable_limit(order, signal, broker)  # noqa: SLF001
            order = await self.execution_engine._normalize_order_for_broker(order, signal, broker)  # noqa: SLF001
            meta = {
                "execution_preflight_stage": "execution_limits",
                "pre_normalized_quantity": str(pre_qty),
                "post_normalized_quantity": str(order.quantity),
                "pre_normalized_limit_price": str(pre_price),
                "post_normalized_limit_price": str(order.limit_price),
            }
            for key in (
                "preflight_capacity_effective_quantity",
                "preflight_capacity_effective_notional",
            ):
                if key in md:
                    meta[key] = str(md[key])
            if order.quantity <= 0:
                return "invalid_quantity_after_normalization", meta
            stale_reason, stale_meta = await self.execution_engine._paper_stale_price_precheck(  # noqa: SLF001
                order,
                signal,
                broker=broker,
                session_factory=session_factory,
            )
            if stale_reason:
                return stale_reason, {**meta, **stale_meta}
            ok = await self.execution_engine._passes_execution_limits(  # noqa: SLF001
                broker,
                order,
                broker_name=str(getattr(signal, "broker", "") or "").strip().lower(),
            )
            if not ok:
                limit_meta = getattr(self.execution_engine, "_last_execution_limit_meta", {}) or {}
                if isinstance(limit_meta, dict):
                    meta.update({str(k): str(v) for k, v in limit_meta.items()})
                return "execution_precheck_rejected", meta
        except Exception as exc:  # noqa: BLE001
            logger.debug("global_edge | execution preflight failed open | {}", exc)
            return None, {}
        return None, {}

    async def _process_signal_global(
        self,
        signal: Any,
        session_factory: Any,
        portfolio_dict: dict[str, Any],
        total_equity: Decimal,
        tradable: Decimal,
        sc_log_buffer: list[dict[str, Any]] | None = None,
    ) -> bool:
        """Single-signal processing mirroring _process_signal for coordinator path."""
        if self.router is None or self.risk_engine is None or self.execution_engine is None:
            return False
        funnel = get_default_funnel_telemetry()
        strategy_key = str(getattr(signal, "strategy", "") or "unknown").strip() or "unknown"
        sig_md = getattr(signal, "metadata", None)
        sig_md = sig_md if isinstance(sig_md, dict) else {}
        is_reduce = bool(
            sig_md.get("reduce_only")
            or sig_md.get("close_only")
            or str(sig_md.get("coordinator_kind", "")).lower() == "trim_symbol"
        )
        if not is_reduce and Decimal(str(self.capital_pct)) <= 0:
            logger.info(
                "global_edge | open skipped because capital allocation is zero | symbol={} strategy={}",
                getattr(signal, "symbol", ""),
                strategy_key,
            )
            funnel.record_execution_blocked(strategy_key)
            if sc_log_buffer is not None:
                sc_log_buffer.append(
                    strategy_candidate_row(
                        symbol=str(getattr(signal, "symbol", "") or ""),
                        strategy=str(getattr(signal, "strategy", "") or ""),
                        side=str(getattr(signal, "side", "") or ""),
                        confidence=float(getattr(signal, "confidence", 0) or 0),
                        status="execution_incomplete",
                        reason="capital_allocation_zero",
                        loop_iteration=self.iterations,
                        metadata={"execution_stage": "pre_risk_zero_allocation_guard"},
                    )
                )
            return False
        preferred_broker = str(sig_md.get("broker") or getattr(signal, "broker", "") or "").strip().lower()
        if is_reduce and preferred_broker and (preferred_broker in self.available_brokers or self.paper_mode):
            routed = preferred_broker
        elif self._can_route_broker(preferred_broker, str(signal.asset_class)) and "broker" in sig_md:
            routed = preferred_broker
        else:
            routed = self.router.route(
                signal.asset_class,
                signal.symbol,
                metadata=sig_md,
            )
        if routed is None:
            funnel.record_execution_blocked(strategy_key)
            return False
        signal.broker = routed
        # Futures execution gate — same as legacy path. See _process_signal().
        if is_futures_symbol(signal.symbol) and os.getenv(
            "FUTURES_EXECUTION_ENABLED", "0"
        ).strip().lower() not in ("1", "true", "yes", "on"):
            if not isinstance(getattr(signal, "metadata", None), dict):
                signal.metadata = {}
            signal.metadata["execution_gated"] = "futures_disabled"
            logger.info(
                "FUTURES DATA-ONLY | skipping execution for {} (set FUTURES_EXECUTION_ENABLED=1)",
                signal.symbol,
            )
            await _persist_signal(
                session_factory,
                signal,
                paper_mode=self.paper_mode,
                timeframe=self.timeframe,
                feature_ts=datetime.now(timezone.utc),
            )
            funnel.record_execution_blocked(strategy_key)
            return False
        native = broker_symbol_for(signal.symbol, routed)
        if native and native != signal.symbol:
            if not isinstance(getattr(signal, "metadata", None), dict):
                signal.metadata = {}
            signal.metadata.setdefault("pipeline_symbol", signal.symbol)
            signal.symbol = native
        self.risk_engine.update_high_watermark(
            Decimal(str(portfolio_dict.get("high_watermark_value", total_equity)))
        )
        self.risk_engine.restore_runtime_state(portfolio_dict)
        risk_decision = await self.risk_engine.evaluate_and_persist(
            session_factory,
            signal,
            portfolio_dict,
        )
        if risk_decision.verdict != RiskVerdict.APPROVED:
            funnel.record_risk_rejected(strategy_key)
            if sc_log_buffer is not None:
                sc_log_buffer.append(
                    strategy_candidate_row(
                        symbol=str(signal.symbol),
                        strategy=str(getattr(signal, "strategy", "") or ""),
                        side=str(getattr(signal, "side", "") or ""),
                        confidence=float(getattr(signal, "confidence", 0) or 0),
                        status="risk_rejected",
                        reason=str(risk_decision.reason or risk_decision.verdict.value),
                        loop_iteration=self.iterations,
                        metadata={
                            "verdict": str(risk_decision.verdict.value),
                            "checks_failed": list(risk_decision.checks_failed or [])[:32],
                        },
                    )
                )
            await _persist_signal(
                session_factory,
                signal,
                paper_mode=self.paper_mode,
                timeframe=self.timeframe,
                feature_ts=datetime.now(timezone.utc),
            )
            return False
        funnel.record_risk_approved(strategy_key)
        await _persist_signal(
            session_factory,
            signal,
            paper_mode=self.paper_mode,
            timeframe=self.timeframe,
            feature_ts=datetime.now(timezone.utc),
        )
        result = await self.execution_engine.execute(
            signal,
            risk_decision,
            session_factory=session_factory,
        )
        if result is None:
            funnel.record_execution_blocked(strategy_key)
            engine_reason = str(
                getattr(self.execution_engine, "last_skip_reason", "") or "execution_no_result"
            )
            engine_meta = getattr(self.execution_engine, "last_skip_metadata", {}) or {}
            if sc_log_buffer is not None:
                sc_log_buffer.append(
                    strategy_candidate_row(
                        symbol=str(signal.symbol),
                        strategy=str(getattr(signal, "strategy", "") or ""),
                        side=str(getattr(signal, "side", "") or ""),
                        confidence=float(getattr(signal, "confidence", 0) or 0),
                        status="execution_incomplete",
                        reason=engine_reason,
                        loop_iteration=self.iterations,
                        metadata={
                            "execution_stage": "no_order_from_engine",
                            "execution_skip_reason": engine_reason,
                            **(engine_meta if isinstance(engine_meta, dict) else {}),
                        },
                    )
                )
            try:
                turnover_hint = float(signal.metadata.get("target_notional", 0.0)) if isinstance(signal.metadata, dict) else 0.0
                liq_hint = float(signal.metadata.get("volume_z_score", 0.0)) if isinstance(signal.metadata, dict) else 0.0
                self.router.record_execution_feedback(
                    broker=str(signal.broker or ""),
                    symbol=str(signal.symbol or ""),
                    filled=False,
                    slippage_bps=None,
                    turnover_hint=turnover_hint,
                    liquidity_hint=liq_hint,
                )
            except Exception:  # noqa: BLE001
                pass
            return False
        status_val = str(getattr(getattr(result, "status", None), "value", getattr(result, "status", ""))).lower()
        if status_val != "filled":
            funnel.record_execution_blocked(strategy_key)
            if sc_log_buffer is not None:
                sc_log_buffer.append(
                    strategy_candidate_row(
                        symbol=str(signal.symbol),
                        strategy=str(getattr(signal, "strategy", "") or ""),
                        side=str(getattr(signal, "side", "") or ""),
                        confidence=float(getattr(signal, "confidence", 0) or 0),
                        status="execution_incomplete",
                        reason=f"order_status_{status_val}",
                        loop_iteration=self.iterations,
                        metadata={"execution_stage": "non_filled", "order_status": status_val},
                    )
                )
            try:
                turnover_hint = float(signal.metadata.get("target_notional", 0.0)) if isinstance(signal.metadata, dict) else 0.0
                liq_hint = float(signal.metadata.get("volume_z_score", 0.0)) if isinstance(signal.metadata, dict) else 0.0
                self.router.record_execution_feedback(
                    broker=str(signal.broker or ""),
                    symbol=str(signal.symbol or ""),
                    filled=False,
                    slippage_bps=None,
                    turnover_hint=turnover_hint,
                    liquidity_hint=liq_hint,
                )
            except Exception:  # noqa: BLE001
                pass
            return False
        filled_qty = Decimal(str(getattr(result, "filled_quantity", "0") or "0"))
        if filled_qty <= 0:
            funnel.record_execution_blocked(strategy_key)
            if sc_log_buffer is not None:
                sc_log_buffer.append(
                    strategy_candidate_row(
                        symbol=str(signal.symbol),
                        strategy=str(getattr(signal, "strategy", "") or ""),
                        side=str(getattr(signal, "side", "") or ""),
                        confidence=float(getattr(signal, "confidence", 0) or 0),
                        status="execution_incomplete",
                        reason="execution_zero_fill",
                        loop_iteration=self.iterations,
                        metadata={"execution_stage": "zero_filled_qty", "order_status": status_val},
                    )
                )
            try:
                turnover_hint = float(signal.metadata.get("target_notional", 0.0)) if isinstance(signal.metadata, dict) else 0.0
                liq_hint = float(signal.metadata.get("volume_z_score", 0.0)) if isinstance(signal.metadata, dict) else 0.0
                self.router.record_execution_feedback(
                    broker=str(signal.broker or ""),
                    symbol=str(signal.symbol or ""),
                    filled=False,
                    slippage_bps=None,
                    turnover_hint=turnover_hint,
                    liquidity_hint=liq_hint,
                )
            except Exception:  # noqa: BLE001
                pass
            return False
        post_trade_state = await _load_portfolio_state(
            session_factory,
            fallback_portfolio_value=total_equity,
            signal_price_fallback=signal.suggested_price,
            capital_pct=Decimal(str(self.capital_pct)),
        )
        fee_dec = Decimal("0")
        fee_raw = getattr(result, "fee", None)
        if fee_raw is not None:
            try:
                fee_dec = Decimal(str(fee_raw))
            except Exception:  # noqa: BLE001
                fee_dec = Decimal("0")
        post_trade_state["fees_today_delta"] = fee_dec
        signal.suggested_quantity = filled_qty
        avg_fill = getattr(result, "avg_fill_price", None)
        if avg_fill is not None:
            try:
                avg_fill_d = Decimal(str(avg_fill))
                if avg_fill_d > 0:
                    signal.suggested_price = avg_fill_d
            except Exception:  # noqa: BLE001
                pass
        _apply_signal_to_portfolio_state(post_trade_state, signal, result)
        await _persist_position_snapshot(session_factory, post_trade_state)
        await _upsert_daily_pnl(session_factory, post_trade_state)
        try:
            slip_bps = None
            avg_fill = getattr(result, "avg_fill_price", None)
            if avg_fill is not None and signal.suggested_price is not None and signal.suggested_price > 0:
                slip_bps = float((Decimal(str(avg_fill)) - Decimal(str(signal.suggested_price))) / Decimal(str(signal.suggested_price)) * Decimal("10000"))
            self.router.record_execution_feedback(
                broker=str(signal.broker or ""),
                symbol=str(signal.symbol or ""),
                filled=True,
                slippage_bps=slip_bps,
                turnover_hint=float(signal.metadata.get("target_notional", 0.0)) if isinstance(signal.metadata, dict) else 0.0,
                liquidity_hint=float(signal.metadata.get("volume_z_score", 0.0)) if isinstance(signal.metadata, dict) else 0.0,
            )
        except Exception:  # noqa: BLE001
            pass
        if sc_log_buffer is not None:
            sc_log_buffer.append(
                strategy_candidate_row(
                    symbol=str(signal.symbol),
                    strategy=str(getattr(signal, "strategy", "") or ""),
                    side=str(getattr(signal, "side", "") or ""),
                    confidence=float(getattr(signal, "confidence", 0) or 0),
                    status="executed",
                    reason="order_filled",
                    loop_iteration=self.iterations,
                )
            )
        funnel.record_execution_approved(strategy_key)
        funnel.record_executed(strategy_key)
        return True

    def _load_mode_cadence_map(self) -> dict[str, int]:
        """Load ``loop_cadence_sec`` from ``config/profile_modes.yaml``.

        The YAML key is optional; when missing/invalid we return an empty
        map and every mode falls back to ``self.loop_interval_sec``.
        """
        try:
            raw = load_yaml("config/profile_modes.yaml")
            mapping = raw.get("loop_cadence_sec") if isinstance(raw, dict) else None
            if not isinstance(mapping, dict):
                return {}
            out: dict[str, int] = {}
            for k, v in mapping.items():
                try:
                    sec = int(v)
                except (TypeError, ValueError):
                    continue
                if sec <= 0:
                    continue
                # Respect the same lower-bound guard as __init__ (10s).
                out[str(k).strip().lower()] = max(10, sec)
            return out
        except Exception as exc:  # noqa: BLE001
            logger.warning("trading_loop | loop_cadence_sec load failed: {}", exc)
            return {}

    @staticmethod
    def _read_active_mode() -> str:
        """Read the operator-selected profile mode. Matches api/server.py semantics."""
        try:
            import json as _json
            from pathlib import Path as _Path

            p = _Path("data/runtime/active_mode.json")
            if p.is_file():
                return str(_json.loads(p.read_text(encoding="utf-8")).get("mode", "trader")).strip().lower()
        except Exception:  # noqa: BLE001
            pass
        return "trader"

    def status_dict(self) -> dict[str, Any]:
        loaded: list[dict[str, Any]] = []
        for name, strat in (self._strategies or {}).items():
            loaded.append({
                "name": name,
                "enabled": bool(getattr(strat, "enabled", True)),
                "kind": "signal",
            })
        arb = self._arb_stack or {}
        # Arbitrage strategy classes don't expose a ``.name`` attribute, so we
        # use stable, human-readable identifiers that mirror the config keys in
        # ``strategies.yaml`` (funding_rate_arbitrage / cross_exchange_arbitrage).
        arb_display = (
            ("funding", "funding_rate_arbitrage"),
            ("cross", "cross_exchange_arbitrage"),
        )
        for key, display in arb_display:
            strat = arb.get(key)
            if strat is None:
                continue
            loaded.append({
                "name": getattr(strat, "name", None) or display,
                "enabled": bool(getattr(strat, "enabled", True)),
                "kind": "arbitrage",
            })
        return {
            "running": self.is_running,
            "iterations": self.iterations,
            "last_iteration_at": self.last_iteration_at.isoformat() if self.last_iteration_at else None,
            "loop_interval_sec": int(self.loop_interval_sec),
            "last_error": self.last_error,
            "paper_mode": self.paper_mode,
            "capital_pct": self.capital_pct,
            "loaded_strategies": loaded,
        }
