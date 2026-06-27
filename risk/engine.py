"""
risk/engine.py
==============
The Risk Engine. The final authority on every trade.

No order enters the market without passing through here.
No exception. No bypass. No override from strategy code.

Architecture:
- Receives a Signal from the signal engine
- Runs all checks in sequence
- Returns APPROVED or REJECTED with reason
- Logs every decision

All thresholds live in config/risk_limits.yaml — editable without code changes.
"""

from dataclasses import dataclass
from copy import deepcopy
from datetime import datetime, timezone
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from enum import Enum
import json
import logging
import os
from pathlib import Path
from typing import Any, Optional

from core.instruments import option_premium_notional, parse_option_contract_from_metadata
from risk.options_env import options_trading_config
from risk.parameters import ParameterManager
from risk.provider import ParameterProvider

logger = logging.getLogger(__name__)


class RiskVerdict(Enum):
    APPROVED = "approved"
    REJECTED = "rejected"


@dataclass
class RiskDecision:
    verdict: RiskVerdict
    reason: str
    signal_id: str
    checks_passed: list[str]
    checks_failed: list[str]


@dataclass(frozen=True)
class RiskPreflightDecision:
    ok: bool
    reason: str
    signal_id: str
    checks_passed: list[str]
    checks_failed: list[str]
    effective_quantity: Decimal
    effective_notional: Decimal


@dataclass
class Signal:
    """Incoming signal from the strategy engine."""
    signal_id: str
    symbol: str
    side: str                       # "buy" or "sell"
    strategy: str
    confidence: float               # 0.0 → 1.0
    suggested_quantity: Decimal
    suggested_price: Optional[Decimal]
    broker: str
    asset_class: str
    timestamp: str
    metadata: dict


class RiskEngine:
    """
    Evaluates every signal before it becomes an order.
    All checks are deterministic and auditable.
    """

    def __init__(self, config: dict):
        self.config = config
        self._parameters = ParameterManager(
            config_path=str(config.get("fundamentals_path", "config/fundamentals.yaml")),
            enable_db_logging=False,
        )
        self._provider = ParameterProvider(
            parameter_manager=self._parameters,
            operational_config=self.config,
            staleness_seconds=int(config.get("parameter_staleness_seconds", 300)),
        )
        self._daily_loss = Decimal("0")
        self._consecutive_losses = 0
        self._cooldown_until: Optional[datetime] = None
        self._open_lock_until: Optional[datetime] = None
        self._open_lock_reason: str = ""
        self._is_killed = False
        self._disabled_brokers: set[str] = set()
        self._disabled_broker_reasons: dict[str, set[str]] = {}
        self._high_watermark = Decimal("0")
        # D125 fix #5 — per-UTC-day cumulative-add tracker. Optimistic
        # (incremented at signal approval, not at fill). Reset on first
        # access after a UTC date change. See `_check_intraday_symbol_adds`
        # and `record_open_signal_notional`.
        self._intraday_added_notional: dict[str, Decimal] = {}
        self._intraday_adds_day_key: str = ""
        explicit_runtime_path = config.get("runtime_state_path") or os.getenv("RISK_RUNTIME_STATE_PATH")
        self._runtime_state_path = Path(str(explicit_runtime_path or "data/runtime/risk_state.json"))
        self._runtime_state_enabled = bool(config.get("persist_runtime_state", True))
        if "PYTEST_CURRENT_TEST" in os.environ and explicit_runtime_path is None:
            self._runtime_state_enabled = False
        self._restore_persisted_runtime_state()

    # ── Public API ────────────────────────────────────────────────────────────

    def evaluate(self, signal: Signal, portfolio_state: dict) -> RiskDecision:
        """
        Run all risk checks on a signal.
        Returns a RiskDecision with APPROVED or REJECTED verdict.
        """

        checks_passed = []
        checks_failed = []
        self._last_signal_symbol = str(getattr(signal, "symbol", "")).strip().upper()

        checks = [
            self._check_kill_switch,
            self._check_broker_disabled,
            self._check_broker_certification,
            self._check_options_trading_policy,
            self._check_m8_symbol_whitelist,
            self._check_m8_strategy_whitelist,
            self._check_m8_max_notional,
            self._check_m8_strategy_sleeve_cap,
            self._check_max_order_notional,
            self._check_allocator_amplification,
            self._check_cooldown,
            self._check_open_lock,
            self._check_asset_proportionality,
            self._check_minimum_order_size,
            self._check_daily_loss_limit,
            self._check_drawdown_limit,
            self._check_position_size,
            self._check_max_loss_per_trade_pct,
            self._check_max_exposure,
            self._check_concentration,
            self._check_asset_class_limits,
            self._check_fx_cluster_exposure,
            self._check_equity_index_cluster_exposure,
            self._check_crypto_cluster_exposure,
            # D125 fix #1 / #5 — single-name notional cap + per-day
            # cumulative-add cap. Hard rails that bind regardless of
            # ``enforce_static_exposure_caps`` (which is False by
            # default and disables every legacy per-symbol limit).
            # Reduce-only signals are exempt inside each check.
            self._check_single_name_notional,
            self._check_intraday_symbol_adds,
            self._check_consecutive_losses,
            self._check_confidence_threshold,
            self._check_theme_uniqueness,
            self._check_catalyst_present,
            self._check_trade_quality_score,
            self._check_crypto_momentum_entry_quality,
        ]

        # D015/global-edge allocation may rank and size candidates, but it is
        # not a substitute for final risk vetoes. Keep the hard business rails
        # live here so an aggressive allocator cannot turn a weak candidate
        # into an oversized order.

        # Reduce-only signals (stop-loss closes, coordinator-driven flatten,
        # explicit reduce_only metadata) must always be allowed to exit a
        # losing position — otherwise drawdown gates trap the book in red ink
        # with no way to release. We still enforce hard gates that apply to
        # any operation regardless of direction (kill switch, broker
        # disabled, options policy, minimum size sanity, arbitrage bundle).
        if self._is_reduce_only_signal(signal):
            # NOTE: ``_check_minimum_order_size`` is deliberately NOT kept here.
            # The minimum-size floor exists to stop trivially-tiny *opens*
            # (dust positions). Applying it to exits is actively harmful: a
            # small/odd-lot residual (e.g. a $30 trim of a name whose class
            # minimum is $50, or a fractional remainder after a partial
            # take-profit) would be REJECTED, so the system could neither
            # trim nor close it — capital trapped with no release valve
            # (audit #5). Exits are bounded by the held position and must
            # always be allowed through the hard rails only.
            keep = {
                self._check_kill_switch,
                self._check_broker_disabled,
                self._check_options_trading_policy,
            }
            checks = [c for c in checks if c in keep]
            logger.info(
                "RISK reduce_only_bypass | %s | %s | running %d gate(s) only",
                signal.signal_id,
                signal.symbol,
                len(checks),
            )

        for check in checks:
            result, label = check(signal, portfolio_state)
            if result:
                checks_passed.append(label)
            else:
                checks_failed.append(label)
                decision = self._reject(signal, f"Failed: {label}", checks_passed, checks_failed)
                logger.warning(f"RISK REJECTED {signal.signal_id} | {label}")
                return decision

        arb_ok, arb_label = self._check_arbitrage_bundle(signal, portfolio_state)
        if not arb_ok:
            checks_failed.append(arb_label)
            decision = self._reject(signal, f"Failed: {arb_label}", checks_passed, checks_failed)
            logger.warning(f"RISK REJECTED {signal.signal_id} | {arb_label}")
            return decision
        checks_passed.append(arb_label)

        # D125.1 — the per-UTC-day cumulative-add tracker is now updated
        # from *actual fills* (execution engine calls
        # ``record_open_signal_notional`` on confirmed fills), not at
        # risk approval. Recording at approval over-counted: approved
        # signals that never fill inflated the daily total and wrongly
        # blocked later trades.

        logger.info(f"RISK APPROVED {signal.signal_id} | {signal.symbol} {signal.side}")
        return RiskDecision(
            verdict=RiskVerdict.APPROVED,
            reason="All checks passed",
            signal_id=signal.signal_id,
            checks_passed=checks_passed,
            checks_failed=[],
        )

    def preflight_capacity(self, signal: Signal, portfolio_state: dict) -> RiskPreflightDecision:
        """Run the same capacity gates used by final risk without persistence.

        This is intentionally a thin wrapper around the production gate methods,
        not a second implementation. It lets upstream allocators discard
        dead-on-arrival opens before they are marked selected, while the final
        ``evaluate`` call remains the authority immediately before execution.
        Clamp-capable checks are run on a cloned signal so preflight can report
        the effective size without mutating the real signal.
        """
        probe = deepcopy(signal)
        checks_passed: list[str] = []
        checks_failed: list[str] = []
        self._last_signal_symbol = str(getattr(probe, "symbol", "")).strip().upper()

        checks = [
            self._check_minimum_order_size,
            self._check_fx_cluster_exposure,
            self._check_equity_index_cluster_exposure,
            self._check_crypto_cluster_exposure,
            self._check_single_name_notional,
            self._check_intraday_symbol_adds,
        ]
        if self._is_reduce_only_signal(probe):
            checks = []

        for check in checks:
            ok, label = check(probe, portfolio_state)
            if ok:
                checks_passed.append(label)
            else:
                checks_failed.append(label)
                return RiskPreflightDecision(
                    ok=False,
                    reason=label,
                    signal_id=str(getattr(signal, "signal_id", "")),
                    checks_passed=checks_passed,
                    checks_failed=checks_failed,
                    effective_quantity=Decimal(str(getattr(probe, "suggested_quantity", "0") or "0")),
                    effective_notional=self._requested_notional(probe),
                )

        return RiskPreflightDecision(
            ok=True,
            reason="preflight_capacity_ok",
            signal_id=str(getattr(signal, "signal_id", "")),
            checks_passed=checks_passed,
            checks_failed=[],
            effective_quantity=Decimal(str(getattr(probe, "suggested_quantity", "0") or "0")),
            effective_notional=self._requested_notional(probe),
        )

    async def evaluate_and_persist(self, session_factory, signal: Signal, portfolio_state: dict) -> RiskDecision:
        """Evaluate a signal and persist the risk decision when DB is available."""
        decision = self.evaluate(signal, portfolio_state)
        await self.persist_decision(session_factory, signal, decision)
        return decision

    def kill(self) -> None:
        """Activate kill switch. Halts all new orders immediately."""
        self._is_killed = True
        self._persist_runtime_state()
        logger.critical("KILL SWITCH ACTIVATED — no new orders will be placed")

    def reset_kill(self) -> None:
        """Deactivate kill switch. Must be deliberate manual action."""
        self._is_killed = False
        self._disabled_brokers.clear()
        self._disabled_broker_reasons.clear()
        self._persist_runtime_state()
        logger.warning("Kill switch deactivated")

    def disable_broker(self, name: str, *, reason: str = "manual") -> None:
        """Stop new orders routed to this broker (execution auto-fail / targeted control)."""
        n = (name or "").strip().lower()
        if not n:
            return
        source = str(reason or "manual").strip().lower()
        self._disabled_brokers.add(n)
        self._disabled_broker_reasons.setdefault(n, set()).add(source)
        self._persist_runtime_state()
        logger.critical(
            "RISK | broker disabled for new orders | broker=%s | reason=%s",
            n,
            source,
        )

    def enable_broker(self, name: str, *, reason: str | None = None) -> None:
        n = (name or "").strip().lower()
        if not n:
            return
        if reason is None:
            self._disabled_brokers.discard(n)
            self._disabled_broker_reasons.pop(n, None)
        else:
            source = str(reason).strip().lower()
            reasons = self._disabled_broker_reasons.get(n, set())
            reasons.discard(source)
            if reasons:
                self._disabled_broker_reasons[n] = reasons
            else:
                self._disabled_broker_reasons.pop(n, None)
                self._disabled_brokers.discard(n)
        self._persist_runtime_state()
        logger.warning("RISK | broker re-enabled | broker=%s | reason=%s", n, reason)

    def broker_disable_reasons(self, name: str) -> frozenset[str]:
        n = (name or "").strip().lower()
        return frozenset(self._disabled_broker_reasons.get(n, set()))

    def is_broker_disabled(self, name: str) -> bool:
        return (name or "").strip().lower() in self._disabled_brokers

    @property
    def disabled_brokers(self) -> frozenset[str]:
        return frozenset(self._disabled_brokers)

    @property
    def is_killed(self) -> bool:
        return self._is_killed

    def record_loss(self, amount: Decimal) -> None:
        """Called by execution engine after a losing trade."""
        self._daily_loss += amount
        self._consecutive_losses += 1
        self._persist_runtime_state()

    def record_win(self) -> None:
        """Called by execution engine after a winning trade."""
        self._consecutive_losses = 0
        self._persist_runtime_state()

    def reset_daily(self) -> None:
        """Called at start of each trading day."""
        self._daily_loss = Decimal("0")
        self._cooldown_until = None
        self._open_lock_until = None
        self._open_lock_reason = ""
        self._persist_runtime_state()

    def update_high_watermark(self, portfolio_value: Decimal) -> None:
        """Track best observed portfolio value for drawdown checks."""
        if portfolio_value > self._high_watermark:
            self._high_watermark = portfolio_value

    def restore_runtime_state(self, portfolio_state: dict) -> None:
        """
        Restore cooldown/loss counters from persisted state.
        This helps preserve safety behavior across process restarts.
        """
        self._daily_loss = self._decimal_from_portfolio(portfolio_state, "daily_loss_accumulated", self._daily_loss)
        try:
            self._consecutive_losses = int(portfolio_state.get("consecutive_losses", self._consecutive_losses))
        except Exception:  # noqa: BLE001
            pass
        raw_cooldown = portfolio_state.get("cooldown_until")
        if isinstance(raw_cooldown, str) and raw_cooldown.strip():
            try:
                dt = datetime.fromisoformat(raw_cooldown.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                self._cooldown_until = dt
            except Exception:  # noqa: BLE001
                pass
        raw_open_lock = portfolio_state.get("open_lock_until")
        if isinstance(raw_open_lock, str) and raw_open_lock.strip():
            try:
                dt = datetime.fromisoformat(raw_open_lock.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                self._open_lock_until = dt
            except Exception:  # noqa: BLE001
                pass
        reason = portfolio_state.get("open_lock_reason")
        if isinstance(reason, str):
            self._open_lock_reason = reason
        if bool(portfolio_state.get("is_killed")):
            self._is_killed = True
        raw_disabled = portfolio_state.get("disabled_brokers")
        raw_reasons = portfolio_state.get("disabled_broker_reasons")
        if isinstance(raw_reasons, dict):
            for name, reasons in raw_reasons.items():
                n = str(name).strip().lower()
                if not n:
                    continue
                if isinstance(reasons, str):
                    sources = {reasons.strip().lower()} if reasons.strip() else set()
                elif isinstance(reasons, (list, tuple, set)):
                    sources = {
                        str(reason).strip().lower()
                        for reason in reasons
                        if str(reason).strip()
                    }
                else:
                    sources = set()
                if sources:
                    self._disabled_brokers.add(n)
                    self._disabled_broker_reasons.setdefault(n, set()).update(sources)
        if isinstance(raw_disabled, (list, tuple, set)):
            for name in raw_disabled:
                n = str(name).strip().lower()
                if not n:
                    continue
                self._disabled_brokers.add(n)
                self._disabled_broker_reasons.setdefault(n, {"legacy"})
        self._persist_runtime_state()

    def snapshot_runtime_state(self) -> dict:
        return {
            "consecutive_losses": int(self._consecutive_losses),
            "daily_loss_accumulated": str(self._daily_loss),
            "cooldown_until": self._cooldown_until.isoformat() if self._cooldown_until else None,
            "open_lock_until": self._open_lock_until.isoformat() if self._open_lock_until else None,
            "open_lock_reason": self._open_lock_reason,
            "is_killed": bool(self._is_killed),
            "disabled_brokers": sorted(self._disabled_brokers),
            "disabled_broker_reasons": {
                name: sorted(reasons)
                for name, reasons in sorted(self._disabled_broker_reasons.items())
            },
        }

    def activate_open_lock(self, *, seconds: float, reason: str) -> None:
        """Temporarily block fresh opens while allowing risk-reducing exits."""
        if seconds <= 0:
            return
        until = datetime.now(timezone.utc) + timedelta(seconds=float(seconds))
        if self._open_lock_until is None or until > self._open_lock_until:
            self._open_lock_until = until
            self._open_lock_reason = str(reason or "drawdown_open_lock")
            self._persist_runtime_state()
            logger.warning(
                "RISK open_lock activated | until=%s | reason=%s",
                until.isoformat(),
                self._open_lock_reason,
            )

    def clear_open_lock(self, reason: str = "recovered") -> None:
        if self._open_lock_until is None:
            return
        logger.warning("RISK open_lock cleared | reason=%s", reason)
        self._open_lock_until = None
        self._open_lock_reason = ""
        self._persist_runtime_state()

    def _restore_persisted_runtime_state(self) -> None:
        if not self._runtime_state_enabled:
            return
        try:
            if not self._runtime_state_path.exists():
                return
            with self._runtime_state_path.open("r", encoding="utf-8") as fh:
                raw = json.load(fh)
            if isinstance(raw, dict):
                self.restore_runtime_state(raw)
        except Exception as exc:  # noqa: BLE001
            logger.warning("risk runtime restore failed | path=%s | %s", self._runtime_state_path, exc)

    def _persist_runtime_state(self) -> None:
        if not self._runtime_state_enabled:
            return
        try:
            self._runtime_state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._runtime_state_path.with_suffix(self._runtime_state_path.suffix + ".tmp")
            payload: dict[str, Any] = self.snapshot_runtime_state()
            payload["updated_at"] = datetime.now(timezone.utc).isoformat()
            with tmp.open("w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2, sort_keys=True)
            tmp.replace(self._runtime_state_path)
        except Exception as exc:  # noqa: BLE001
            logger.warning("risk runtime persist failed | path=%s | %s", self._runtime_state_path, exc)

    # ── Checks ────────────────────────────────────────────────────────────────

    def _check_kill_switch(self, signal, portfolio) -> tuple[bool, str]:
        return (not self._is_killed, "kill_switch")

    def _check_broker_disabled(self, signal, portfolio) -> tuple[bool, str]:
        name = (getattr(signal, "broker", None) or "").strip().lower()
        if name and name in self._disabled_brokers:
            return False, "broker_disabled"
        return True, "broker_operational"

    def _check_open_lock(self, signal, portfolio) -> tuple[bool, str]:
        if self._is_reduce_only_signal(signal):
            return (True, "drawdown_open_lock")
        if self._open_lock_until is None:
            return (True, "drawdown_open_lock")
        now = datetime.now(timezone.utc)
        if now >= self._open_lock_until:
            self._open_lock_until = None
            self._open_lock_reason = ""
            self._persist_runtime_state()
            return (True, "drawdown_open_lock")
        meta = signal.metadata if isinstance(getattr(signal, "metadata", None), dict) else {}
        if bool(meta.get("hedge") or meta.get("protective_hedge")):
            return (True, "drawdown_open_lock")
        if self._open_lock_allows_target_redeployment(signal, portfolio, meta):
            logger.info(
                "RISK drawdown_open_lock ALLOW redeploy | %s | exposure=%s target=%s proposed=%s until=%s reason=%s",
                getattr(signal, "symbol", ""),
                self._decimal_from_portfolio(portfolio, "current_gross_exposure", Decimal("0")),
                self._decimal_from_portfolio(portfolio, "tradable_capital", Decimal("0")),
                self._open_lock_signal_notional(signal, meta),
                self._open_lock_until.isoformat(),
                self._open_lock_reason,
            )
            return (True, "drawdown_open_lock")
        logger.warning(
            "RISK drawdown_open_lock REJECT | %s | until=%s reason=%s",
            getattr(signal, "symbol", ""),
            self._open_lock_until.isoformat(),
            self._open_lock_reason,
        )
        return (False, "drawdown_open_lock")

    def _open_lock_signal_notional(self, signal, meta: dict[str, Any]) -> Decimal:
        for key in ("target_notional", "sizing_final_action_capital", "notional", "capital"):
            raw = meta.get(key)
            if raw is None or raw == "":
                continue
            try:
                value = Decimal(str(raw)).copy_abs()
            except (InvalidOperation, TypeError, ValueError):
                continue
            if value > 0:
                return value
        try:
            qty = Decimal(str(getattr(signal, "suggested_quantity", "0") or "0")).copy_abs()
            px = Decimal(str(getattr(signal, "suggested_price", "0") or "0")).copy_abs()
        except (InvalidOperation, TypeError, ValueError):
            return Decimal("0")
        return qty * px if qty > 0 and px > 0 else Decimal("0")

    def _open_lock_allows_target_redeployment(self, signal, portfolio, meta: dict[str, Any]) -> bool:
        if not bool(meta.get("allocation_selected")):
            return False
        target = self._decimal_from_portfolio(portfolio, "tradable_capital", Decimal("0"))
        exposure = self._decimal_from_portfolio(portfolio, "current_gross_exposure", Decimal("0"))
        if target <= 0 or exposure >= target:
            return False
        proposed = self._open_lock_signal_notional(signal, meta)
        if proposed <= 0:
            return False
        return proposed <= (target - exposure)

    def _check_broker_certification(self, signal, portfolio) -> tuple[bool, str]:
        """D127 P2 — only Certified-tier brokers may place trades.

        Experimental connectors may inform but never execute; paper-only
        connectors may not execute in live mode. Reduce-only signals are
        exempt — exits must always be allowed regardless of tier (and the
        reduce-only path already filters this gate out of the check set).
        Fail-open on catalogue/infrastructure glitches — see
        ``connectors.certification.broker_execution_decision``.
        """
        cfg = self.config.get("connector_certification")
        cfg = cfg if isinstance(cfg, dict) else {}
        if not bool(cfg.get("enforce", True)):
            return (True, "broker_certification")
        if self._is_reduce_only_signal(signal):
            return (True, "broker_certification")
        broker = (getattr(signal, "broker", None) or "").strip().lower()
        if not broker:
            return (True, "broker_certification")
        try:
            from connectors.certification import broker_execution_decision

            live = (os.getenv("APP_ENV", "paper") or "paper").strip().lower() == "live"
            allowed, reason = broker_execution_decision(broker, system_live_mode=live)
        except Exception as exc:  # noqa: BLE001 — gate must never crash the engine
            logger.warning("RISK broker_certification | check error (%s); allowing", exc)
            return (True, "broker_certification")
        if not allowed:
            logger.warning(
                "RISK broker_certification REJECT | broker=%s reason=%s", broker, reason
            )
            return (False, f"broker_certification:{reason}")
        return (True, "broker_certification")

    @staticmethod
    def _is_reduce_only_signal(signal: Signal) -> bool:
        """Detect close/exit/stop-loss intent so the engine can skip soft gates.

        A *reduce-only* signal is one that, by construction, can only
        decrease net exposure. We trust four sources of truth (in order):

        1. ``signal.metadata.reduce_only`` (explicit operator/runtime flag)
        2. ``signal.metadata.coordinator_kind`` starting with ``close`` /
           ``flatten`` / ``exit`` (D015 global-edge coordinator emits these)
        3. ``signal.strategy`` named ``stop_loss_monitor`` (D033 monitor)
        4. ``signal.signal_id`` prefixed with ``stoploss-`` (legacy path)

        Any of these implies the order is protective and must not be
        blocked by drawdown / exposure / quality gates that exist to limit
        *new* risk-taking.
        """
        meta = signal.metadata if isinstance(getattr(signal, "metadata", None), dict) else {}
        if bool(meta.get("reduce_only")):
            return True
        ck = str(meta.get("coordinator_kind", "")).strip().lower()
        if ck.startswith("close") or ck.startswith("flatten") or ck.startswith("exit"):
            return True
        strat = str(getattr(signal, "strategy", "") or "").strip().lower()
        if strat == "stop_loss_monitor":
            return True
        sid = str(getattr(signal, "signal_id", "") or "").strip().lower()
        if sid.startswith("stoploss-") or sid.startswith("stop_loss-"):
            return True
        return False

    @staticmethod
    def _is_option_signal(signal: Signal) -> bool:
        ac = (signal.asset_class or "").strip().lower()
        if ac == "option":
            return True
        meta = signal.metadata if isinstance(getattr(signal, "metadata", None), dict) else {}
        if meta.get("instrument_type") == "option":
            return True
        return isinstance(meta.get("option_contract"), dict)

    def _check_options_trading_policy(self, signal: Signal, portfolio: dict) -> tuple[bool, str]:
        """
        Conservative IBKR single-leg options gate (config ``options_trading`` + env).
        Does not run for non-option signals.
        """
        if not self._is_option_signal(signal):
            return (True, "options_skipped")

        cfg = options_trading_config(self.config)
        ok_label = "options_trading_policy"

        if not cfg["enabled"]:
            logger.warning("RISK options_disabled | signal_id=%s", signal.signal_id)
            return (False, "options_disabled")

        if cfg["paper_only"]:
            env_mode = (os.getenv("APP_ENV", "paper") or "paper").strip().lower()
            if env_mode == "live":
                logger.warning("RISK options_paper_only | signal_id=%s", signal.signal_id)
                return (False, "options_paper_only")

        meta = signal.metadata if isinstance(getattr(signal, "metadata", None), dict) else {}
        spec = parse_option_contract_from_metadata(meta)
        if spec is None:
            logger.warning("RISK options_invalid_spec | signal_id=%s", signal.signal_id)
            return (False, "options_invalid_spec")

        allowed = {str(x).strip().upper() for x in cfg["allowed_underlyings"] if str(x).strip()}
        und = spec.underlying_symbol.strip().upper()
        if und not in allowed:
            logger.warning(
                "RISK options_underlying_denied | underlying=%s | signal_id=%s",
                und,
                signal.signal_id,
            )
            return (False, "options_underlying_denied")

        qty_abs = abs(signal.suggested_quantity)
        try:
            max_contracts = int(cfg["max_contracts_per_trade"])
        except Exception:  # noqa: BLE001
            max_contracts = 1
        if qty_abs > Decimal(max_contracts):
            logger.warning(
                "RISK options_max_contracts | qty=%s max=%s | signal_id=%s",
                qty_abs,
                max_contracts,
                signal.signal_id,
            )
            return (False, "options_max_contracts")

        px = self._resolve_signal_price(signal)
        if px <= 0:
            logger.warning("RISK options_missing_premium | signal_id=%s", signal.signal_id)
            return (False, "options_missing_premium")

        mult = int(spec.multiplier)
        premium = option_premium_notional(qty_abs, px, mult)
        if premium > cfg["max_premium_per_trade"]:
            logger.warning(
                "RISK options_max_premium_per_trade | premium=%s cap=%s | signal_id=%s",
                premium,
                cfg["max_premium_per_trade"],
                signal.signal_id,
            )
            return (False, "options_max_premium_per_trade")

        side = (signal.side or "").strip().lower()
        pos_map = portfolio.get("positions") if isinstance(portfolio.get("positions"), dict) else {}
        sym_key = spec.position_key()
        row = pos_map.get(sym_key) if sym_key else None
        prev_qty = Decimal("0")
        if isinstance(row, dict):
            try:
                prev_qty = Decimal(str(row.get("quantity", "0")))
            except Exception:  # noqa: BLE001
                prev_qty = Decimal("0")

        if side == "sell":
            if not cfg["allow_sell_to_close"]:
                logger.warning("RISK options_sell_disabled | signal_id=%s", signal.signal_id)
                return (False, "options_sell_disabled")
            if prev_qty <= 0:
                logger.warning(
                    "RISK options_short_opening_rejected | signal_id=%s",
                    signal.signal_id,
                )
                return (False, "options_short_opening_rejected")

        current_opt = self._decimal_from_portfolio(portfolio, "option_premium_exposure", Decimal("0"))
        if side == "buy":
            projected = current_opt + premium
            max_tot = cfg["max_total_premium_exposure"]
            if projected > max_tot:
                logger.warning(
                    "RISK options_max_total_premium_exposure | projected=%s cap=%s | signal_id=%s",
                    projected,
                    max_tot,
                    signal.signal_id,
                )
                return (False, "options_max_total_premium_exposure")

        return (True, ok_label)

    def _m8_guards_active(self) -> bool:
        m8 = self.config.get("m8_micro_live")
        if not isinstance(m8, dict) or not m8.get("enabled"):
            return False
        return os.getenv("APP_ENV", "paper").strip().lower() == "live"

    def _check_m8_symbol_whitelist(self, signal, portfolio) -> tuple[bool, str]:
        if self._is_option_signal(signal):
            return (True, "m8_symbol_whitelist")
        if not self._m8_guards_active():
            return (True, "m8_symbol_whitelist")
        m8 = self.config["m8_micro_live"]
        wl = m8.get("symbol_whitelist") or []
        if not wl:
            return (True, "m8_symbol_whitelist")
        sym = str(getattr(signal, "symbol", "") or "").strip().upper()
        allowed = {str(x).strip().upper() for x in wl}
        return (sym in allowed, "m8_symbol_whitelist")

    def _check_m8_strategy_whitelist(self, signal, portfolio) -> tuple[bool, str]:
        if self._is_option_signal(signal):
            return (True, "m8_strategy_whitelist")
        if not self._m8_guards_active():
            return (True, "m8_strategy_whitelist")
        m8 = self.config["m8_micro_live"]
        wl = m8.get("strategy_whitelist") or []
        if not wl:
            return (True, "m8_strategy_whitelist")
        st = str(getattr(signal, "strategy", "") or "").strip()
        allowed = {str(x).strip() for x in wl}
        return (st in allowed, "m8_strategy_whitelist")

    def _check_m8_max_notional(self, signal, portfolio) -> tuple[bool, str]:
        if self._is_option_signal(signal):
            return (True, "m8_max_notional")
        if not self._m8_guards_active():
            return (True, "m8_max_notional")
        m8 = self.config["m8_micro_live"]
        n = self._requested_notional(signal)
        caps: list[Decimal] = []
        cap_usd = m8.get("max_notional_usd_per_order")
        if cap_usd is not None:
            try:
                d = Decimal(str(cap_usd))
                if d > 0:
                    caps.append(d)
            except Exception:  # noqa: BLE001
                pass
        cap_gbp = m8.get("max_notional_gbp_per_order")
        if cap_gbp is not None:
            try:
                gbp = Decimal(str(cap_gbp))
                fx = Decimal(str(os.getenv("M8_GBP_USD_RATE", "1.25")))
                if gbp > 0 and fx > 0:
                    caps.append(gbp * fx)
            except Exception:  # noqa: BLE001
                pass
        if not caps:
            return (True, "m8_max_notional")
        limit = min(caps)
        return (n <= limit, "m8_max_notional")

    def _check_m8_strategy_sleeve_cap(self, signal, portfolio) -> tuple[bool, str]:
        """
        Optional per-strategy order caps under M8 (separate sleeves / allocation).
        See config/m8_micro_live.yaml strategy_sleeve_caps.
        """
        if self._is_option_signal(signal):
            return (True, "m8_strategy_sleeve_cap")
        if not self._m8_guards_active():
            return (True, "m8_strategy_sleeve_cap")
        m8 = self.config["m8_micro_live"]
        caps = m8.get("strategy_sleeve_caps")
        if not isinstance(caps, dict):
            return (True, "m8_strategy_sleeve_cap")
        st = str(getattr(signal, "strategy", "") or "").strip()
        entry = caps.get(st)
        if not isinstance(entry, dict):
            return (True, "m8_strategy_sleeve_cap")
        pct = entry.get("max_order_notional_pct_of_portfolio")
        if pct is None:
            return (True, "m8_strategy_sleeve_cap")
        try:
            p = Decimal(str(pct))
        except Exception:  # noqa: BLE001
            return (True, "m8_strategy_sleeve_cap")
        if p <= 0:
            return (True, "m8_strategy_sleeve_cap")
        pv = self._sizing_nav(portfolio)
        if pv <= 0:
            return (False, "m8_strategy_sleeve_cap")
        limit = pv * p
        n = self._requested_notional(signal)
        return (n <= limit, "m8_strategy_sleeve_cap")

    def _check_max_order_notional(self, signal, portfolio) -> tuple[bool, str]:
        """Optional legacy fixed per-order notional cap."""
        if not bool(self.config.get("enforce_static_order_caps", False)):
            return (True, "max_order_notional")
        if self._is_option_signal(signal):
            return (True, "max_order_notional")
        n = self._requested_notional(signal)
        caps: list[Decimal] = []
        raw_abs = self.config.get("max_order_notional_usd")
        if raw_abs is not None:
            try:
                d = Decimal(str(raw_abs))
                if d > 0:
                    caps.append(d)
            except Exception:  # noqa: BLE001
                pass
        raw_pct = self.config.get("max_order_notional_pct")
        if raw_pct is not None:
            try:
                p = Decimal(str(raw_pct))
                base = self._sizing_nav(portfolio)
                if p > 0 and base > 0:
                    caps.append(base * p)
            except Exception:  # noqa: BLE001
                pass
        if not caps:
            return (True, "max_order_notional")
        return (n <= min(caps), "max_order_notional")

    def _check_allocator_amplification(self, signal, portfolio) -> tuple[bool, str]:
        """Optional legacy cap on allocator redistribution vs strategy intent."""
        if not bool(self.config.get("enforce_static_order_caps", False)):
            return (True, "allocator_amplification")
        if self._is_option_signal(signal) or self._is_reduce_only_signal(signal):
            return (True, "allocator_amplification")
        try:
            max_mult = Decimal(str(self.config.get("max_allocator_notional_multiple", "0")))
        except Exception:  # noqa: BLE001
            max_mult = Decimal("0")
        if max_mult <= 0:
            return (True, "allocator_amplification")
        meta = signal.metadata if isinstance(getattr(signal, "metadata", None), dict) else {}
        raw_base = (
            meta.get("sizing_strategy_target_notional")
            or meta.get("sizing_pre_mode_capital")
            or meta.get("strategy_target_notional")
        )
        if raw_base is None:
            return (True, "allocator_amplification")
        try:
            base = Decimal(str(raw_base))
        except Exception:  # noqa: BLE001
            return (True, "allocator_amplification")
        if base <= 0:
            return (True, "allocator_amplification")
        return (self._requested_notional(signal) <= base * max_mult, "allocator_amplification")

    def _check_cooldown(self, signal, portfolio) -> tuple[bool, str]:
        if self._cooldown_until is None:
            return (True, "cooldown")
        if datetime.now(timezone.utc) < self._cooldown_until:
            return (False, "cooldown")
        self._cooldown_until = None
        return (True, "cooldown")

    def _check_daily_loss_limit(self, signal, portfolio) -> tuple[bool, str]:
        portfolio_value = self._decimal_from_portfolio(portfolio, "portfolio_value", Decimal("0"))
        if portfolio_value <= 0:
            return (False, "daily_loss_limit")
        base_max_daily_loss_pct = self._provider.get_decimal("max_daily_loss_pct", fallback=Decimal("0"))
        
        # D120: Dynamic NAV loss guardrails based on market state
        market_state_score = Decimal("1.0")
        if isinstance(portfolio, dict):
            pmeta = portfolio.get("metadata", {})
            if isinstance(pmeta, dict) and "market_state_score" in pmeta:
                try:
                    market_state_score = Decimal(str(pmeta["market_state_score"]))
                except (TypeError, ValueError, InvalidOperation):
                    pass
                    
        # In terrible regimes, shrink allowed loss budget down to 10% of base
        max_daily_loss_pct = base_max_daily_loss_pct * max(Decimal("0.1"), min(Decimal("1.0"), market_state_score))
        
        # Use whichever loss tracker is worse: runtime or provided portfolio state.
        stated_pnl = self._decimal_from_portfolio(portfolio, "daily_realized_pnl", Decimal("0"))
        state_loss = abs(stated_pnl) if stated_pnl < 0 else Decimal("0")
        observed_loss = max(self._daily_loss, state_loss)
        allowed_loss = portfolio_value * max_daily_loss_pct
        return (observed_loss <= allowed_loss, "daily_loss_limit")

    def _check_position_size(self, signal, portfolio) -> tuple[bool, str]:
        if not bool(self.config.get("enforce_static_exposure_caps", False)):
            return (True, "position_size")
        sizing_base = self._sizing_nav(portfolio)
        if sizing_base <= 0:
            return (False, "position_size")
        max_position_pct = self._effective_max_position_pct()
        price = self._resolve_signal_price(signal)
        if price <= 0:
            return (False, "position_size")
        requested_notional = abs(signal.suggested_quantity) * price
        allowed_notional = sizing_base * max_position_pct
        return (requested_notional <= allowed_notional, "position_size")

    def _check_asset_proportionality(self, signal, portfolio) -> tuple[bool, str]:
        sizing_base = self._sizing_nav(portfolio)
        if sizing_base <= 0:
            return (False, "asset_proportionality")
        threshold_pct = self._provider.get_decimal("proportionality_threshold_pct", fallback=Decimal("0"))
        minimum = self._minimum_order_size(signal.asset_class)
        if minimum <= 0:
            return (True, "asset_proportionality")
        return (minimum < (sizing_base * threshold_pct), "asset_proportionality")

    def _check_minimum_order_size(self, signal, portfolio) -> tuple[bool, str]:
        if self._is_option_signal(signal):
            return (True, "minimum_order_size")
        notional = self._requested_notional(signal)
        minimum = self._minimum_order_size(signal.asset_class)
        if minimum <= 0:
            return (True, "minimum_order_size")
        return (notional >= minimum, "minimum_order_size")

    def _check_drawdown_limit(self, signal, portfolio) -> tuple[bool, str]:
        portfolio_value = self._decimal_from_portfolio(portfolio, "portfolio_value", Decimal("0"))
        if portfolio_value <= 0:
            return (False, "drawdown_limit")
        state_hwm = self._decimal_from_portfolio(portfolio, "high_watermark_value", Decimal("0"))
        if state_hwm > self._high_watermark:
            self._high_watermark = state_hwm
        if self._high_watermark <= 0:
            self._high_watermark = portfolio_value
            return (True, "drawdown_limit")
        base_max_drawdown_pct = self._provider.get_decimal("max_drawdown_pct", fallback=Decimal("0"))
        
        drawdown = (self._high_watermark - portfolio_value) / self._high_watermark
        if isinstance(getattr(signal, "metadata", None), dict):
            signal.metadata["risk_drawdown_pct"] = str(drawdown)
            signal.metadata["risk_drawdown_limit_pct"] = str(base_max_drawdown_pct)
        return (drawdown <= base_max_drawdown_pct, "drawdown_limit")

    def _check_max_loss_per_trade_pct(self, signal, portfolio) -> tuple[bool, str]:
        """Pre-trade loss-budget gate.

        **D031E scope note.** This function is a *pre-open* check: it refuses
        to approve a NEW signal whose expected loss (notional × stop distance
        proxy) would exceed ``portfolio_value * max_loss_per_trade_pct``.

        It does NOT enforce the loss budget on an already-open position that
        drifts into the red after entry. That post-open monitor is scaffolded
        in ``risk/stop_loss.py::evaluate_stop_loss`` but not yet wired to a
        runtime task. See ``docs/DECISIONS.md`` D031 for the rollout plan.
        """
        portfolio_value = self._decimal_from_portfolio(portfolio, "portfolio_value", Decimal("0"))
        if portfolio_value <= 0:
            return (False, "max_loss_per_trade_pct")
        base_max_loss_pct = self._provider.get_decimal("max_loss_per_trade_pct", fallback=Decimal("0"))
        
        market_state_score = Decimal("1.0")
        if isinstance(portfolio, dict):
            pmeta = portfolio.get("metadata", {})
            if isinstance(pmeta, dict) and "market_state_score" in pmeta:
                try:
                    market_state_score = Decimal(str(pmeta["market_state_score"]))
                except (TypeError, ValueError, InvalidOperation):
                    pass
        
        max_loss_pct = base_max_loss_pct * max(Decimal("0.1"), min(Decimal("1.0"), market_state_score))
        requested_notional = self._requested_notional(signal)
        expected_loss_pct = self._infer_expected_loss_pct(signal)
        if expected_loss_pct is None:
            # Keep the gate non-blocking when stop-distance proxy is unavailable.
            return (True, "max_loss_per_trade_pct")
        expected_loss = requested_notional * expected_loss_pct
        allowed_loss = portfolio_value * max_loss_pct
        return (expected_loss <= allowed_loss, "max_loss_per_trade_pct")

    def _check_max_exposure(self, signal, portfolio) -> tuple[bool, str]:
        if not bool(self.config.get("enforce_static_exposure_caps", False)):
            return (True, "max_exposure")
        sizing_base = self._sizing_nav(portfolio)
        if sizing_base <= 0:
            return (False, "max_exposure")
        max_gross_pct = self._provider.get_decimal("max_gross_exposure_pct", fallback=Decimal("0"))
        current_gross = self._decimal_from_portfolio(portfolio, "current_gross_exposure", Decimal("0"))
        projected_gross = current_gross + self._requested_notional(signal)
        allowed_gross = sizing_base * max_gross_pct
        return (projected_gross <= allowed_gross, "max_exposure")

    def _check_concentration(self, signal, portfolio) -> tuple[bool, str]:
        if not bool(self.config.get("enforce_static_exposure_caps", False)):
            return (True, "concentration")
        sizing_base = self._sizing_nav(portfolio)
        if sizing_base <= 0:
            return (False, "concentration")
        max_concentration_pct = self._provider.get_decimal("max_concentration_pct", fallback=Decimal("0"))
        symbol_exposure_raw = portfolio.get("symbol_exposure", {})
        symbol_exposure = Decimal("0")
        meta = signal.metadata if isinstance(getattr(signal, "metadata", None), dict) else {}
        spec = parse_option_contract_from_metadata(meta)
        sym_key = spec.position_key() if spec is not None else signal.symbol
        if isinstance(symbol_exposure_raw, dict):
            symbol_exposure = Decimal(str(symbol_exposure_raw.get(sym_key, "0")))
        projected_symbol_exposure = symbol_exposure + self._requested_notional(signal)
        allowed_symbol_exposure = sizing_base * max_concentration_pct
        return (projected_symbol_exposure <= allowed_symbol_exposure, "concentration")

    def _check_asset_class_limits(self, signal, portfolio) -> tuple[bool, str]:
        if not bool(self.config.get("enforce_static_exposure_caps", False)):
            return (True, "asset_class_limit")
        sizing_base = self._sizing_nav(portfolio)
        if sizing_base <= 0:
            return (False, "asset_class_limit")

        asset_class = (signal.asset_class or "").strip().lower()
        asset_class_exposure_raw = portfolio.get("asset_class_exposure", {})
        asset_class_exposure = Decimal("0")
        if isinstance(asset_class_exposure_raw, dict):
            asset_class_exposure = Decimal(str(asset_class_exposure_raw.get(asset_class, "0")))
        projected = asset_class_exposure + self._requested_notional(signal)

        key_by_class = {
            "crypto": "max_crypto_pct",
            "bond": "max_bond_pct",
            # Portfolio-level cap for all equity exposure combined.
            "equity": "max_equity_pct",
            "forex": "max_forex_pct",
            "fx": "max_forex_pct",
            "future": "max_future_pct",
            "option": "max_option_pct",
        }
        config_key = key_by_class.get(asset_class)
        if config_key is None:
            logger.warning(
                "Asset class '%s' has no configured limit key; allowing by default",
                asset_class,
            )
            return (True, "asset_class_limit")

        pct = self._cfg_decimal(config_key, fallback=Decimal("1.0"))
        allowed = sizing_base * pct
        return (projected <= allowed, "asset_class_limit")

    # ------------------------------------------------------------------
    # D115 — FX directional cluster cap.
    #
    # Six FX positions all betting the same way on USD looked like six
    # independent risks in the position book but were one concentrated bet.
    # This check bounds the aggregate signed USD exposure across all held
    # forex positions plus the proposed signal.
    # ------------------------------------------------------------------
    def _check_fx_cluster_exposure(self, signal, portfolio) -> tuple[bool, str]:
        # Opt-in via ``config/risk_limits.yaml::fx_cluster.enabled``. Default
        # OFF so unrelated legacy tests / experimental runs that pre-date
        # this gate behave identically; production YAML turns it on.
        cfg = self.config.get("fx_cluster") or {}
        if not bool(cfg.get("enabled", False)):
            return (True, "fx_cluster")

        asset_class = (getattr(signal, "asset_class", "") or "").strip().lower()
        if asset_class not in ("forex", "fx"):
            return (True, "fx_cluster")

        # Exits are never blocked by cluster caps.
        if self._is_reduce_only_signal(signal):
            return (True, "fx_cluster")

        sizing_base = self._sizing_nav(portfolio)
        if sizing_base <= 0:
            return (True, "fx_cluster")

        max_pct = self._regime_scaled_cap_pct(
            cfg=cfg,
            key="max_usd_directional_exposure_pct",
            default="0.15",
            portfolio=portfolio,
            signal=signal,
        )

        if max_pct <= 0:
            return (True, "fx_cluster")
        cap_notional = sizing_base * max_pct

        proposed = self._fx_usd_exposure_from_signal(signal)
        if proposed == 0:
            return (True, "fx_cluster")

        current = Decimal("0")
        positions = portfolio.get("positions", {}) if isinstance(portfolio, dict) else {}
        if isinstance(positions, dict):
            for sym, pos in positions.items():
                if not isinstance(pos, dict):
                    continue
                current += self._fx_usd_exposure_from_position(str(sym), pos)

        projected = current + proposed
        abs_current = abs(current)
        abs_projected = abs(projected)

        # Never block a position that NEUTRALISES the existing cluster
        # (i.e. reduces |signed USD exposure|).
        if abs_projected <= abs_current:
            return (True, "fx_cluster")

        return (abs_projected <= cap_notional, "fx_cluster")

    @staticmethod
    def _fx_pair_orientation(symbol: str) -> int:
        """
        Return +1 if symbol is USDxxx (long = long USD),
        -1 if xxxUSD (long = short USD),
        0 otherwise (no direct USD leg, e.g. EURGBP).
        """
        sym = (symbol or "").strip().upper().replace("/", "").replace("-", "").replace("=X", "")
        if "USD" not in sym:
            return 0
        if sym.startswith("USD"):
            return 1
        if sym.endswith("USD"):
            return -1
        return 0

    @classmethod
    def _fx_usd_exposure_from_position(cls, symbol: str, pos: dict) -> Decimal:
        orient = cls._fx_pair_orientation(symbol)
        if orient == 0:
            return Decimal("0")
        try:
            qty = Decimal(str(pos.get("quantity", "0") or "0"))
        except (InvalidOperation, TypeError, ValueError):
            return Decimal("0")
        if qty == 0:
            return Decimal("0")
        price_raw = (
            pos.get("current_price")
            or pos.get("avg_entry_price")
            or pos.get("price")
            or "0"
        )
        try:
            price = Decimal(str(price_raw))
        except (InvalidOperation, TypeError, ValueError):
            return Decimal("0")
        if price <= 0:
            return Decimal("0")
        magnitude = abs(qty) * price
        side_sign = 1 if qty > 0 else -1
        return Decimal(orient * side_sign) * magnitude

    @classmethod
    def _fx_usd_exposure_from_signal(cls, signal) -> Decimal:
        orient = cls._fx_pair_orientation(getattr(signal, "symbol", ""))
        if orient == 0:
            return Decimal("0")
        try:
            qty = Decimal(str(getattr(signal, "suggested_quantity", "0") or "0"))
        except (InvalidOperation, TypeError, ValueError):
            return Decimal("0")
        if qty == 0:
            return Decimal("0")
        side = (getattr(signal, "side", "") or "").strip().lower()
        if side in ("buy", "long"):
            side_sign = 1
        elif side in ("sell", "short"):
            side_sign = -1
        else:
            return Decimal("0")
        price = (
            getattr(signal, "suggested_price", None)
            or (signal.metadata or {}).get("last_price")
            if hasattr(signal, "metadata") and isinstance(getattr(signal, "metadata", None), dict)
            else getattr(signal, "suggested_price", None)
        )
        try:
            price_d = Decimal(str(price)) if price is not None else Decimal("0")
        except (InvalidOperation, TypeError, ValueError):
            return Decimal("0")
        if price_d <= 0:
            return Decimal("0")
        magnitude = abs(qty) * price_d
        return Decimal(orient * side_sign) * magnitude

    # ------------------------------------------------------------------
    # D115 — Broad-market equity index cluster cap.
    #
    # Symmetric to ``_check_fx_cluster_exposure`` but for the US equity
    # index family (SPY/QQQ/IWM/DIA/...). SPY long + QQQ short + IWM short
    # share systematic equity-beta risk and are not three independent
    # bets. This bounds the aggregate signed notional within the cluster.
    # ------------------------------------------------------------------
    def _check_equity_index_cluster_exposure(self, signal, portfolio) -> tuple[bool, str]:
        cfg = self.config.get("equity_index_cluster") or {}
        if not bool(cfg.get("enabled", False)):
            return (True, "equity_index_cluster")

        cluster_syms = self._normalize_symbol_list(cfg.get("symbols") or [])
        if not cluster_syms:
            return (True, "equity_index_cluster")

        signal_sym = (getattr(signal, "symbol", "") or "").strip().upper()
        if signal_sym not in cluster_syms:
            return (True, "equity_index_cluster")

        if self._is_reduce_only_signal(signal):
            return (True, "equity_index_cluster")

        sizing_base = self._sizing_nav(portfolio)
        if sizing_base <= 0:
            return (True, "equity_index_cluster")

        max_pct = self._regime_scaled_cap_pct(
            cfg=cfg,
            key="max_net_exposure_pct",
            default="0.20",
            portfolio=portfolio,
            signal=signal,
        )
        if max_pct <= 0:
            return (True, "equity_index_cluster")
        cap_notional = sizing_base * max_pct

        # Signed proposed delta from the new signal.
        side = (getattr(signal, "side", "") or "").strip().lower()
        if side in ("buy", "long"):
            sign = 1
        elif side in ("sell", "short"):
            sign = -1
        else:
            return (True, "equity_index_cluster")
        try:
            qty = Decimal(str(getattr(signal, "suggested_quantity", "0") or "0"))
            price = Decimal(str(getattr(signal, "suggested_price", None) or "0"))
        except (InvalidOperation, TypeError, ValueError):
            return (True, "equity_index_cluster")
        if qty <= 0 or price <= 0:
            return (True, "equity_index_cluster")
        proposed = Decimal(sign) * abs(qty) * price

        # Current signed cluster exposure.
        current = Decimal("0")
        positions = portfolio.get("positions", {}) if isinstance(portfolio, dict) else {}
        if isinstance(positions, dict):
            for sym, pos in positions.items():
                norm = str(sym or "").strip().upper()
                if norm not in cluster_syms or not isinstance(pos, dict):
                    continue
                try:
                    p_qty = Decimal(str(pos.get("quantity", "0") or "0"))
                    p_px = Decimal(str(pos.get("current_price") or pos.get("avg_entry_price") or "0"))
                except (InvalidOperation, TypeError, ValueError):
                    continue
                if p_qty == 0 or p_px <= 0:
                    continue
                current += p_qty * p_px  # qty already carries sign

        projected = current + proposed
        if abs(projected) <= abs(current):
            return (True, "equity_index_cluster")
        return (abs(projected) <= cap_notional, "equity_index_cluster")

    # ------------------------------------------------------------------
    # D131 — Crypto cluster cap.
    # Symmetric to ``_check_equity_index_cluster_exposure`` but for the
    # crypto family across ALL venues (kraken/binance/bybit). BTC long
    # on kraken + ETH long on binance + SOL long on bybit look like
    # three independent single-name bets each below the 5 % per-name
    # cap, but they share the same systematic crypto-beta risk and lose
    # together when the asset class drops. This cap bounds the aggregate
    # signed crypto notional across every venue. Reduce-only never
    # blocked; neutralising legs (those that REDUCE the absolute cluster
    # exposure) always pass.
    # ------------------------------------------------------------------
    def _check_crypto_cluster_exposure(self, signal, portfolio) -> tuple[bool, str]:
        cfg = self.config.get("crypto_cluster") or {}
        if not bool(cfg.get("enabled", False)):
            return (True, "crypto_cluster")

        if self._is_reduce_only_signal(signal):
            return (True, "crypto_cluster")

        # Identify the signal as crypto. Prefer the explicit asset_class;
        # fall back to the canonical "-USD" suffix used for spot pairs
        # (BTC-USD, ETH-USD, ...).
        sig_asset = str(getattr(signal, "asset_class", "") or "").strip().lower()
        sig_sym = (getattr(signal, "symbol", "") or "").strip().upper()
        is_crypto_signal = sig_asset == "crypto" or sig_sym.endswith("-USD")
        if not is_crypto_signal:
            return (True, "crypto_cluster")

        sizing_base = self._sizing_nav(portfolio)
        if sizing_base <= 0:
            return (True, "crypto_cluster")

        try:
            max_pct = self._regime_scaled_cap_pct(
                cfg=cfg,
                key="max_net_exposure_pct",
                default="0.10",
                portfolio=portfolio,
                signal=signal,
            )
        except (InvalidOperation, TypeError, ValueError):
            max_pct = Decimal("0.10")
        if max_pct <= 0:
            return (True, "crypto_cluster")
        cap_notional = sizing_base * max_pct

        # Signed proposed delta from the new signal.
        side = (getattr(signal, "side", "") or "").strip().lower()
        if side in ("buy", "long"):
            sign = 1
        elif side in ("sell", "short"):
            sign = -1
        else:
            return (True, "crypto_cluster")
        try:
            qty = Decimal(str(getattr(signal, "suggested_quantity", "0") or "0"))
            price = Decimal(str(getattr(signal, "suggested_price", None) or "0"))
        except (InvalidOperation, TypeError, ValueError):
            return (True, "crypto_cluster")
        if qty <= 0 or price <= 0:
            return (True, "crypto_cluster")
        proposed = Decimal(sign) * abs(qty) * price

        # Current signed crypto exposure across every venue.
        current = Decimal("0")
        positions = portfolio.get("positions", {}) if isinstance(portfolio, dict) else {}
        if isinstance(positions, dict):
            for pos_key, pos in positions.items():
                if not isinstance(pos, dict):
                    continue
                p_asset = str(pos.get("asset_class", "") or "").strip().lower()
                p_sym = str(pos.get("symbol") or str(pos_key).split(":", 1)[-1]).strip().upper()
                if not (p_asset == "crypto" or p_sym.endswith("-USD")):
                    continue
                try:
                    p_qty = Decimal(str(pos.get("quantity", "0") or "0"))
                    p_px = Decimal(str(pos.get("current_price") or pos.get("avg_entry_price") or "0"))
                except (InvalidOperation, TypeError, ValueError):
                    continue
                if p_qty == 0 or p_px <= 0:
                    continue
                current += p_qty * p_px  # qty carries sign

        projected = current + proposed
        # A neutralising leg (one that REDUCES the absolute cluster
        # exposure) must always be allowed through — it is risk-reducing.
        if abs(projected) <= abs(current):
            return (True, "crypto_cluster")
        if abs(projected) <= cap_notional:
            return (True, "crypto_cluster")
        remaining = cap_notional - abs(current)
        if remaining > 0 and abs(proposed) > remaining and price > 0:
            clamped_qty = remaining / price
            if clamped_qty > 0:
                if not isinstance(getattr(signal, "metadata", None), dict):
                    signal.metadata = {}
                signal.suggested_quantity = clamped_qty
                signal.metadata["risk_crypto_cluster_clamped"] = True
                signal.metadata["risk_crypto_cluster_existing_notional"] = str(current)
                signal.metadata["risk_crypto_cluster_cap_notional"] = str(cap_notional)
                signal.metadata["risk_crypto_cluster_requested_notional"] = str(abs(proposed))
                signal.metadata["risk_crypto_cluster_effective_notional"] = str(remaining)
                logger.info(
                    "RISK crypto_cluster CLAMP | %s %s | proposed=%s -> %s current_cluster=%s cap=%s",
                    sig_sym,
                    side,
                    str(abs(proposed)),
                    str(remaining),
                    str(current),
                    str(cap_notional),
                )
                return (True, "crypto_cluster")
        logger.warning(
            "RISK crypto_cluster REJECT | %s %s | proposed=%s current_cluster=%s "
            "projected=%s cap=%s",
            sig_sym, side, str(proposed), str(current), str(projected), str(cap_notional),
        )
        return (False, "crypto_cluster")

    @staticmethod
    def _normalize_symbol_list(raw) -> set[str]:
        out: set[str] = set()
        if isinstance(raw, (list, tuple, set)):
            for v in raw:
                s = str(v or "").strip().upper()
                if s:
                    out.add(s)
        return out

    def _regime_scaled_cap_pct(
        self,
        *,
        cfg: dict,
        key: str,
        default: str,
        portfolio: dict,
        signal: Signal,
    ) -> Decimal:
        try:
            base = Decimal(str(cfg.get(key, default)))
        except (InvalidOperation, TypeError, ValueError):
            base = Decimal(default)
        base = max(Decimal("0"), min(Decimal("1"), base))
        scalar = Decimal(str(self._market_state_score(portfolio, signal, default=1.0)))
        return max(Decimal("0"), min(base, base * scalar))

    def _clamp_signal_to_notional(self, signal, allowed_notional: Decimal) -> bool:
        """D125.1 — resize a signal so its notional fits ``allowed_notional``.

        Used by the single-name and per-day caps to **clamp** an oversized
        order down to the cap rather than vetoing it outright — a position
        limit must bound exposure, not block deployment. Returns True when
        the signal was clamped (or already within), False when it cannot
        be sized (no usable price). Never enlarges a signal.
        """
        if allowed_notional <= 0:
            return False
        price = self._resolve_signal_price(signal)
        if price <= 0:
            return False
        try:
            current_qty = abs(Decimal(str(getattr(signal, "suggested_quantity", 0) or 0)))
        except (InvalidOperation, TypeError, ValueError):
            current_qty = Decimal("0")
        new_qty = allowed_notional / price
        if current_qty > 0 and new_qty >= current_qty:
            return True  # already within the cap — nothing to clamp
        signal.suggested_quantity = new_qty
        meta = signal.metadata if isinstance(getattr(signal, "metadata", None), dict) else None
        if meta is not None and "risk_notional_override" in meta:
            meta["risk_notional_override"] = str(allowed_notional)
        return True

    # ------------------------------------------------------------------
    # D125 fix #1 — Single-name notional cap.
    #
    # Hard ceiling on per-symbol exposure as a fraction of NAV, evaluated
    # at signal-submission time. Enforced UNCONDITIONALLY (does not
    # consult ``enforce_static_exposure_caps``), because the legacy
    # ``max_single_stock_pct`` is documented as inert by default and the
    # 2026-05-21 BF-B audit showed a single common equity reaching 28.5%
    # of NAV via 38 consecutive volume_flow buys without any cap firing.
    # The intended "adaptive sizing replaces fixed caps" philosophy has
    # no portfolio-awareness inside the per-signal sizer, so a hard
    # boundary at the risk engine is required.
    #
    # Reduce-only signals are exempt — exits must always be allowed
    # through, especially on names already past the cap.
    # ------------------------------------------------------------------
    def _check_single_name_notional(self, signal, portfolio) -> tuple[bool, str]:
        cfg = self.config.get("single_name_notional") or {}
        if not bool(cfg.get("enabled", True)):
            return (True, "single_name_notional")

        if self._is_reduce_only_signal(signal):
            return (True, "single_name_notional")

        # Arbitrage bundles are evaluated separately by
        # `_check_arbitrage_bundle`; per-leg notional doesn't represent
        # the true exposure of the bundle.
        if self._is_arbitrage_signal(signal):
            return (True, "single_name_notional")

        # Options are option-premium denominated and have a separate
        # `options_trading` policy gate; skip here.
        if self._is_option_signal(signal):
            return (True, "single_name_notional")

        try:
            max_pct = Decimal(str(cfg.get("max_pct_nav", "0.05")))
        except (InvalidOperation, TypeError, ValueError):
            max_pct = Decimal("0.05")
        if max_pct <= 0:
            return (True, "single_name_notional")

        sizing_base = self._sizing_nav(portfolio)
        if sizing_base <= 0:
            return (True, "single_name_notional")

        cap_notional = sizing_base * max_pct
        signal_sym = (getattr(signal, "symbol", "") or "").strip().upper()
        if not signal_sym:
            return (True, "single_name_notional")

        proposed = self._requested_notional(signal)
        if proposed <= 0:
            return (True, "single_name_notional")

        # Existing position quantity on the same symbol (signed)
        existing_qty = Decimal("0")
        positions = portfolio.get("positions", {}) if isinstance(portfolio, dict) else {}
        price = Decimal("0")
        if isinstance(positions, dict):
            for pos_key, pos in positions.items():
                if not isinstance(pos, dict):
                    continue
                k_sym = str(pos.get("symbol") or pos_key).split(":", 1)[-1].strip().upper()
                if k_sym != signal_sym:
                    continue
                try:
                    qty = Decimal(str(pos.get("quantity", "0") or "0"))
                except (InvalidOperation, TypeError, ValueError):
                    continue
                if qty == 0:
                    continue
                existing_qty += qty
                price_raw = (
                    pos.get("current_price")
                    or pos.get("avg_entry_price")
                    or pos.get("price")
                    or "0"
                )
                try:
                    price = Decimal(str(price_raw))
                except (InvalidOperation, TypeError, ValueError):
                    continue

        if price <= 0:
            price = self._resolve_signal_price(signal)

        if price <= 0:
            return (True, "single_name_notional")

        # Determine signal side sign
        side = (getattr(signal, "side", "") or "").strip().lower()
        if side in ("buy", "long"):
            sig_sign = 1
        elif side in ("sell", "short"):
            sig_sign = -1
        else:
            return (True, "single_name_notional")

        try:
            suggested_qty = Decimal(str(getattr(signal, "suggested_quantity", "0") or "0"))
        except (InvalidOperation, TypeError, ValueError):
            return (True, "single_name_notional")

        proposed_qty_signed = Decimal(sig_sign) * suggested_qty
        projected_qty = existing_qty + proposed_qty_signed

        # Pure position reduction (no sign change, size decreases or stays the same)
        is_pure_reduction = (existing_qty * projected_qty >= 0) and (abs(projected_qty) <= abs(existing_qty))
        if existing_qty != 0 and is_pure_reduction:
            meta = signal.metadata if isinstance(getattr(signal, "metadata", None), dict) else None
            if meta is not None:
                meta["risk_position_reducing"] = True
            return (True, "single_name_notional")

        # Projected exposure notional
        projected_notional = abs(projected_qty) * price
        if projected_notional <= cap_notional:
            return (True, "single_name_notional")

        # Clamp down to cap room
        max_projected_qty = Decimal(sig_sign) * (cap_notional / price)
        allowed_qty_signed = max_projected_qty - existing_qty

        # If allowed qty is in the opposite direction of the signal, reject
        if (allowed_qty_signed * Decimal(sig_sign)) <= 0:
            logger.warning(
                "RISK single_name_notional REJECT | %s | existing=%s already at/over cap=%s",
                signal_sym, str(abs(existing_qty) * price), str(cap_notional),
            )
            return (False, "single_name_notional")

        allowed_notional = abs(allowed_qty_signed) * price
        if not self._clamp_signal_to_notional(signal, allowed_notional):
            return (False, "single_name_notional")

        meta = signal.metadata if isinstance(getattr(signal, "metadata", None), dict) else None
        if meta is not None:
            meta["risk_single_name_topup_clamped"] = True
            meta["risk_single_name_existing_notional"] = str(abs(existing_qty) * price)
            meta["risk_single_name_cap_notional"] = str(cap_notional)
        logger.info(
            "RISK single_name_notional CLAMP | %s | proposed=%s -> %s (%.2f%% of NAV %s)",
            signal_sym, str(proposed), str(allowed_notional),
            float(max_pct * Decimal("100")), str(sizing_base),
        )
        return (True, "single_name_notional")

    # ------------------------------------------------------------------
    # D125 fix #5 — Per-UTC-day cumulative-add cap per symbol.
    #
    # A complement to the single-name cap above. The 2026-05-21 audit
    # found 38 BF-B buy signals over 35 hours, each individually under
    # the per-action cap but jointly compounding to a 28% concentration.
    # This bounds the cumulative net-add notional per symbol per UTC
    # day; reduce-only is exempt.
    #
    # Bookkeeping is optimistic (incremented at risk APPROVAL not at
    # fill) so the cap is conservative — if approved signals don't
    # actually fill, the tracker overestimates and the cap binds a
    # tick earlier than strictly necessary. That's the safe direction
    # for a defensive limit.
    # ------------------------------------------------------------------
    def _check_intraday_symbol_adds(self, signal, portfolio) -> tuple[bool, str]:
        cfg = self.config.get("intraday_symbol_adds") or {}
        if not bool(cfg.get("enabled", True)):
            return (True, "intraday_symbol_adds")

        if self._is_reduce_only_signal(signal):
            return (True, "intraday_symbol_adds")
        if self._is_arbitrage_signal(signal):
            return (True, "intraday_symbol_adds")
        if self._is_option_signal(signal):
            return (True, "intraday_symbol_adds")

        try:
            max_pct = Decimal(str(cfg.get("max_pct_nav", "0.10")))
        except (InvalidOperation, TypeError, ValueError):
            max_pct = Decimal("0.10")
        if max_pct <= 0:
            return (True, "intraday_symbol_adds")

        sizing_base = self._sizing_nav(portfolio)
        if sizing_base <= 0:
            return (True, "intraday_symbol_adds")

        cap_notional = sizing_base * max_pct
        signal_sym = (getattr(signal, "symbol", "") or "").strip().upper()
        if not signal_sym:
            return (True, "intraday_symbol_adds")

        proposed = self._requested_notional(signal)
        if proposed <= 0:
            return (True, "intraday_symbol_adds")

        # Existing position quantity on the same symbol (signed)
        existing_qty = Decimal("0")
        positions = portfolio.get("positions", {}) if isinstance(portfolio, dict) else {}
        price = Decimal("0")
        if isinstance(positions, dict):
            for pos_key, pos in positions.items():
                if not isinstance(pos, dict):
                    continue
                k_sym = str(pos.get("symbol") or pos_key).split(":", 1)[-1].strip().upper()
                if k_sym != signal_sym:
                    continue
                try:
                    qty = Decimal(str(pos.get("quantity", "0") or "0"))
                except (InvalidOperation, TypeError, ValueError):
                    continue
                if qty == 0:
                    continue
                existing_qty += qty
                price_raw = (
                    pos.get("current_price")
                    or pos.get("avg_entry_price")
                    or pos.get("price")
                    or "0"
                )
                try:
                    price = Decimal(str(price_raw))
                except (InvalidOperation, TypeError, ValueError):
                    continue

        if price <= 0:
            price = self._resolve_signal_price(signal)

        if price <= 0:
            return (True, "intraday_symbol_adds")

        # Determine signal side sign
        side = (getattr(signal, "side", "") or "").strip().lower()
        if side in ("buy", "long"):
            sig_sign = 1
        elif side in ("sell", "short"):
            sig_sign = -1
        else:
            return (True, "intraday_symbol_adds")

        try:
            suggested_qty = Decimal(str(getattr(signal, "suggested_quantity", "0") or "0"))
        except (InvalidOperation, TypeError, ValueError):
            return (True, "intraday_symbol_adds")

        proposed_qty_signed = Decimal(sig_sign) * suggested_qty
        projected_qty = existing_qty + proposed_qty_signed

        # Pure position reduction (no sign change, size decreases or stays the same)
        is_pure_reduction = (existing_qty * projected_qty >= 0) and (abs(projected_qty) <= abs(existing_qty))
        if existing_qty != 0 and is_pure_reduction:
            meta = signal.metadata if isinstance(getattr(signal, "metadata", None), dict) else None
            if meta is not None:
                meta["risk_position_reducing"] = True
            return (True, "intraday_symbol_adds")

        # Determine net added exposure notional for today
        if existing_qty * projected_qty >= 0:
            added_notional = (abs(projected_qty) - abs(existing_qty)) * price
        else:
            added_notional = abs(projected_qty) * price

        if added_notional <= 0:
            return (True, "intraday_symbol_adds")

        self._roll_intraday_adds_day_if_needed()
        already = self._intraday_added_notional.get(signal_sym, Decimal("0"))
        projected_added_notional = already + added_notional
        if projected_added_notional <= cap_notional:
            return (True, "intraday_symbol_adds")

        # Clamp down to daily room left
        allowed_added_notional = cap_notional - already
        if allowed_added_notional <= 0:
            logger.warning(
                "RISK intraday_symbol_adds REJECT | %s | added_today=%s already at/over cap=%s",
                signal_sym, str(already), str(cap_notional),
            )
            return (False, "intraday_symbol_adds")

        if existing_qty * projected_qty >= 0:
            allowed_qty_signed = Decimal(sig_sign) * (allowed_added_notional / price)
        else:
            target_projected_qty = Decimal(sig_sign) * (allowed_added_notional / price)
            allowed_qty_signed = target_projected_qty - existing_qty

        allowed_notional_clamp = abs(allowed_qty_signed) * price
        if not self._clamp_signal_to_notional(signal, allowed_notional_clamp):
            logger.warning(
                "RISK intraday_symbol_adds REJECT | %s | added_today=%s already at/over cap=%s",
                signal_sym, str(already), str(cap_notional),
            )
            return (False, "intraday_symbol_adds")

        logger.info(
            "RISK intraday_symbol_adds CLAMP | %s | proposed=%s -> %s (%.2f%% of NAV %s)",
            signal_sym, str(proposed), str(allowed_notional_clamp),
            float(max_pct * Decimal("100")), str(sizing_base),
        )
        return (True, "intraday_symbol_adds")

    def _roll_intraday_adds_day_if_needed(self) -> None:
        """Reset the per-day cumulative-add tracker on UTC date change."""
        today = datetime.now(timezone.utc).date().isoformat()
        if today != self._intraday_adds_day_key:
            self._intraday_added_notional.clear()
            self._intraday_adds_day_key = today

    def record_open_signal_notional(self, symbol: str, notional: Decimal) -> None:
        """Update the optimistic per-day cumulative-add tracker.

        Called from ``evaluate()`` right after all gates pass (and the
        signal is about to be returned APPROVED). Reduce-only signals
        are not recorded — they reduce exposure, not add to it.
        """
        try:
            sym = (symbol or "").strip().upper()
            if not sym:
                return
            d = Decimal(str(notional))
            if d <= 0:
                return
            self._roll_intraday_adds_day_if_needed()
            self._intraday_added_notional[sym] = (
                self._intraday_added_notional.get(sym, Decimal("0")) + d
            )
        except Exception:  # noqa: BLE001
            pass

    def _check_consecutive_losses(self, signal, portfolio) -> tuple[bool, str]:
        max_losses = int(self.config.get("max_consecutive_losses", 0))
        if max_losses > 0:
            market_state_score = 1.0
            vol_scalar = 1.0
            if isinstance(portfolio, dict):
                pmeta = portfolio.get("metadata")
                if isinstance(pmeta, dict):
                    if "market_state_score" in pmeta:
                        try:
                            market_state_score = float(pmeta["market_state_score"])
                        except (TypeError, ValueError):
                            pass
                    if "market_volatility_scalar" in pmeta:
                        try:
                            vol_scalar = float(pmeta["market_volatility_scalar"])
                        except (TypeError, ValueError):
                            pass

            # D120: Dynamic max_consecutive_losses scaling based on regime & volatility
            multiplier = market_state_score / max(1.0, vol_scalar)
            multiplier = max(0.2, min(1.0, multiplier))
            scaled_max_losses = max(1, int(round(max_losses * multiplier)))

            if self._consecutive_losses >= scaled_max_losses:
                base_cooldown_minutes = int(self.config.get("cooldown_minutes", 0))
                # Higher volatility = longer cooldown (wait for the chop to subside)
                cooldown_minutes = int(base_cooldown_minutes * (vol_scalar if vol_scalar > 0 else 1.0))
                
                self._cooldown_until = datetime.now(timezone.utc) + timedelta(minutes=max(0, cooldown_minutes))
                self._persist_runtime_state()
                return (False, "consecutive_losses")
        return (True, "consecutive_losses")


    def _check_confidence_threshold(self, signal, portfolio) -> tuple[bool, str]:
        min_confidence = self._dynamic_quality_threshold(
            base_key="min_signal_confidence",
            block_key="confidence",
            portfolio=portfolio,
            signal=signal,
            default=1.0,
        )
        if isinstance(getattr(signal, "metadata", None), dict):
            signal.metadata["risk_min_confidence_dynamic"] = round(float(min_confidence), 4)
        return (signal.confidence >= min_confidence, "confidence_threshold")

    def _check_theme_uniqueness(self, signal, portfolio) -> tuple[bool, str]:
        """
        Reject if we already hold a position in the same symbol in the same direction.
        One idea = one expression. No stacking the same thesis.
        """
        if not self.config.get("theme_uniqueness_check", True):
            return (True, "theme_uniqueness")
        positions = portfolio.get("positions", {})
        if not positions:
            return (True, "theme_uniqueness")
        meta = signal.metadata if isinstance(getattr(signal, "metadata", None), dict) else {}
        if bool(meta.get("risk_single_name_topup_clamped")) or (
            bool(meta.get("sizing_topup_existing"))
            and str(meta.get("coordinator_kind", "")).lower() == "open_strategy"
        ):
            return (True, "theme_uniqueness")
        spec = parse_option_contract_from_metadata(meta)
        sym = spec.position_key() if spec is not None else (getattr(signal, "symbol", "") or "").strip().upper()
        for pos_sym, pos in positions.items():
            if pos_sym.strip().upper() != sym:
                continue
            qty = float(pos.get("quantity", 0) or 0)
            if qty == 0:
                continue
            # Already long → reject another buy; already short → reject another sell
            if signal.side == "buy" and qty > 0:
                return (False, "theme_uniqueness")
            if signal.side == "sell" and qty < 0:
                return (False, "theme_uniqueness")
        return (True, "theme_uniqueness")

    def _check_catalyst_present(self, signal, portfolio) -> tuple[bool, str]:
        """
        A trade needs a reason to exist.
        Require at least one of: confirmed volume spike OR meaningful news sentiment.
        If neither is available (no data), pass through — don't penalise missing data.
        """
        if not self.config.get("require_catalyst", False):
            return (True, "catalyst_present")

        meta = signal.metadata if isinstance(signal.metadata, dict) else {}
        volume_z = float(meta.get("volume_z_score") or 0.0)
        news_score = float(signal.news_score or 0.0)
        volume_threshold = float(self.config.get("catalyst_volume_z_min", 1.0))
        news_threshold = float(self.config.get("catalyst_news_score_min", 0.15))

        # If neither metric is populated, data is absent — assume neutral, don't block
        data_absent = (volume_z == 0.0 and news_score == 0.0)
        if data_absent:
            return (True, "catalyst_present")

        volume_confirms = volume_z >= volume_threshold
        news_confirms = abs(news_score) >= news_threshold
        return (volume_confirms or news_confirms, "catalyst_present")

    def _check_trade_quality_score(self, signal, portfolio) -> tuple[bool, str]:
        """
        Composite quality gate. Raises the bar on what constitutes a 'real' trade.

        score = 0.45 * confidence
              + 0.30 * news_factor     (|news_score| capped at 1; 0 if no news configured)
              + 0.25 * volume_factor   (volume_z normalized to 0-1; 0.5 if no data)

        Modes can raise the threshold (defender) or lower it (hunter).
        Set min_trade_quality_score: 0 to disable entirely.
        """
        base_threshold = float(self.config.get("min_trade_quality_score", 0.0))
        if base_threshold <= 0.0:
            return (True, "trade_quality")
        threshold = self._dynamic_quality_threshold(
            base_key="min_trade_quality_score",
            block_key="trade_quality",
            portfolio=portfolio,
            signal=signal,
            default=0.0,
        )

        confidence = float(getattr(signal, "confidence", 0.0) or 0.0)
        news_raw = float(getattr(signal, "news_score", None) or 0.0)
        # Always work with the real signal.metadata dict — never a copy.
        # {} is falsy so `signal.metadata or {}` would create a new dict and lose the write-back.
        if not isinstance(getattr(signal, "metadata", None), dict):
            signal.metadata = {}
        meta = signal.metadata
        volume_z = float(meta.get("volume_z_score") or 0.0)

        # News factor: 0 when absent, normalized to 0-1 when present
        news_factor = min(1.0, abs(news_raw))

        # Determine quality based on available data signals
        if volume_z == 0.0 and news_raw == 0.0:
            # No enrichment data — weight entirely on confidence
            quality = confidence
        elif volume_z == 0.0:
            # Have news, no volume
            quality = 0.55 * confidence + 0.45 * news_factor
        elif news_raw == 0.0:
            # Have volume, no news
            volume_factor = min(1.0, max(0.0, volume_z / 3.0))  # z=3 → 1.0
            quality = 0.65 * confidence + 0.35 * volume_factor
        else:
            # Full data — all three components
            volume_factor = min(1.0, max(0.0, volume_z / 3.0))
            quality = 0.45 * confidence + 0.30 * news_factor + 0.25 * volume_factor

        # Write quality score back into the signal's live metadata dict (persisted after risk check)
        try:
            meta["trade_quality_score"] = round(quality, 4)
            meta["risk_min_trade_quality_dynamic"] = round(float(threshold), 4)
        except Exception:  # noqa: BLE001
            pass

        passed = quality >= threshold
        if not passed:
            logger.debug(
                "RISK quality_gate | {} | conf={:.2f} news={:.2f} vol_z={:.2f} → quality={:.3f} < threshold={:.2f}",
                signal.symbol, confidence, abs(news_raw), volume_z, quality, threshold,
            )
        return (passed, "trade_quality")

    def _check_crypto_momentum_entry_quality(self, signal, portfolio) -> tuple[bool, str]:
        """Hard gate for crypto momentum entries with shadow-model red flags.

        D175: ASR-USD / ATM-USD showed a failure mode where a single
        momentum-breakout source, zero news, shadow meta-label DROP, and
        broken/high-risk microstructure were still allowed through because the
        warnings were advisory. Keep this gate narrow: opening crypto longs
        sourced from momentum/orchestrator only.
        """
        cfg = self.config.get("crypto_momentum_entry_quality") or {}
        if not bool(cfg.get("enabled", True)):
            return (True, "crypto_momentum_entry_quality")
        if self._is_reduce_only_signal(signal):
            return (True, "crypto_momentum_entry_quality")
        asset_class = str(getattr(signal, "asset_class", "") or "").strip().lower()
        if asset_class != "crypto":
            return (True, "crypto_momentum_entry_quality")
        side = str(getattr(signal, "side", "") or "").strip().lower()
        if side != "buy":
            return (True, "crypto_momentum_entry_quality")
        meta = signal.metadata if isinstance(getattr(signal, "metadata", None), dict) else {}
        coordinator_kind = str(meta.get("coordinator_kind", "")).strip().lower()
        if coordinator_kind != "open_strategy" and not bool(meta.get("orchestrator")):
            return (True, "crypto_momentum_entry_quality")
        contributing = str(meta.get("contributing_strategies") or getattr(signal, "strategy", "") or "").lower()
        if "momentum" not in contributing and "breakout" not in contributing:
            return (True, "crypto_momentum_entry_quality")

        reasons: list[str] = []

        if bool(cfg.get("block_meta_label_drop", True)):
            if bool(meta.get("meta_label_shadow")) and meta.get("meta_label_kept") is False:
                reasons.append("meta_label_drop")

        if bool(cfg.get("block_bad_microstructure", True)):
            label = str(meta.get("microstructure_shadow_label", "") or "").strip().lower()
            raw_reasons = str(meta.get("microstructure_shadow_reasons", "") or "").lower()
            shadow_reason = str(meta.get("microstructure_shadow_reason", "") or "").lower()
            shadow_error = str(meta.get("microstructure_shadow_error", "") or "").lower()
            bad_labels = {str(x).lower() for x in cfg.get("bad_microstructure_labels", ["high_risk"])}
            bad_reasons = {str(x).lower() for x in cfg.get(
                "bad_microstructure_reasons",
                ["malformed_book", "missing_spread", "unknown_asset_pair", "fetch_or_score_failed"],
            )}
            if label in bad_labels:
                reasons.append(f"microstructure_{label}")
            if any(r in raw_reasons for r in bad_reasons):
                reasons.append("microstructure_bad_reason")
            if "unknown asset pair" in shadow_error or "unknown_asset_pair" in shadow_error:
                reasons.append("microstructure_unknown_pair")
            if "fetch_or_score_failed" in shadow_reason and bool(cfg.get("block_microstructure_fetch_failure", True)):
                reasons.append("microstructure_fetch_failed")

        if bool(cfg.get("require_second_source_confirmation", True)):
            news_abs = abs(self._float_from_any(meta.get("ai_news_score", getattr(signal, "news_score", 0.0)), 0.0))
            trend_ok = self._boolish(meta.get("trend_confirms") or meta.get("trend_following_confirms"))
            forecast_ok = self._float_from_any(meta.get("forecast_expected_return"), 0.0) > 0
            trained_keep = bool(meta.get("meta_label_kept") is True and not bool(meta.get("meta_label_shadow")))
            if news_abs < self._float_cfg(cfg, "min_news_abs", 0.15) and not (trend_ok or forecast_ok or trained_keep):
                reasons.append("single_source_no_confirmation")

        if bool(cfg.get("block_overextended", True)):
            rsi = self._float_from_any(meta.get("rsi_14"), 0.0)
            bbp = self._float_from_any(
                meta.get("bbp_20_2") or meta.get("BBP_20_2.0_2.0") or meta.get("bbp"),
                0.0,
            )
            if rsi >= self._float_cfg(cfg, "max_rsi_14", 82.0) and bbp >= self._float_cfg(cfg, "max_bbp", 1.20):
                reasons.append("overextended")

        if not reasons and bool(cfg.get("stamp_structural_stop_metadata", True)):
            if "stop_loss_atr" not in meta:
                meta["stop_loss_atr"] = str(cfg.get("structural_stop_atr_mult", "1.5"))
            if "atr_pct" not in meta and meta.get("garch_vol_1d") is not None:
                meta["atr_pct"] = str(meta.get("garch_vol_1d"))

        if reasons:
            try:
                meta["crypto_momentum_entry_quality_reasons"] = sorted(set(reasons))
            except Exception:  # noqa: BLE001
                pass
            return (False, "crypto_momentum_entry_quality")
        return (True, "crypto_momentum_entry_quality")

    def _dynamic_quality_threshold(
        self,
        *,
        base_key: str,
        block_key: str,
        portfolio: dict,
        signal: Signal,
        default: float,
    ) -> float:
        try:
            base = float(self.config.get(base_key, default))
        except (TypeError, ValueError):
            base = float(default)
        cfg = self.config.get("dynamic_quality_thresholds") or {}
        if not isinstance(cfg, dict) or not bool(cfg.get("enabled", True)):
            return base
        block = cfg.get(block_key) or {}
        if not isinstance(block, dict):
            block = {}
        lo = self._float_cfg(block, "min", 0.0)
        hi = self._float_cfg(block, "max", 1.0)
        if hi < lo:
            lo, hi = hi, lo
        pivot = self._float_cfg(block, "win_rate_pivot", 0.55)
        wr_weight = self._float_cfg(block, "win_rate_weight", 0.35)
        ms_weight = self._float_cfg(block, "market_state_weight", 0.25)
        deployment_weight = self._float_cfg(block, "deployment_pressure_weight", 0.0)

        mss = self._market_state_score(portfolio, signal, default=1.0)
        win_rate = self._recent_win_rate(portfolio, signal)
        pressure = 0.0
        if isinstance(getattr(signal, "metadata", None), dict):
            try:
                pressure = float(signal.metadata.get("deployment_pressure", 0.0) or 0.0)
            except (TypeError, ValueError):
                pressure = 0.0
        pressure = max(0.0, min(1.0, pressure))
        threshold = base
        threshold += base * ms_weight * max(0.0, 1.0 - mss)
        if win_rate is not None:
            threshold += base * wr_weight * max(-1.0, min(1.0, pivot - win_rate))
        threshold -= deployment_weight * pressure
        floor = lo
        meta = signal.metadata if isinstance(getattr(signal, "metadata", None), dict) else {}
        if bool(meta.get("allocation_selected")) and pressure > 0.0 and deployment_weight > 0.0:
            floor = max(0.0, lo - deployment_weight * pressure)
            try:
                meta[f"risk_{block_key}_floor_dynamic"] = round(float(floor), 4)
            except Exception:  # noqa: BLE001
                pass
        return max(floor, min(hi, threshold))

    @staticmethod
    def _float_cfg(cfg: dict, key: str, default: float) -> float:
        try:
            return float(cfg.get(key, default))
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _float_from_any(value: Any, default: float = 0.0) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _boolish(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if value is None:
            return False
        return str(value).strip().lower() in {"1", "true", "yes", "on", "y", "pass", "passed"}

    def _recent_win_rate(self, portfolio: dict, signal: Signal | None = None) -> float | None:
        candidates: list[Any] = []
        strategy_names: list[str] = []
        if signal is not None:
            for raw_name in (
                getattr(signal, "strategy", None),
                getattr(signal, "strategy_name", None),
            ):
                if raw_name:
                    strategy_names.append(str(raw_name).strip())
            if isinstance(getattr(signal, "metadata", None), dict):
                for key in ("strategy_name", "coordinator_strategy_name", "source_strategy"):
                    raw_name = signal.metadata.get(key)
                    if raw_name:
                        strategy_names.append(str(raw_name).strip())
        strategy_keys = {
            name.strip().lower()
            for name in strategy_names
            if name and name.strip()
        }

        def _strategy_candidates(container: Any) -> list[Any]:
            if not isinstance(container, dict) or not strategy_keys:
                return []
            out: list[Any] = []
            for raw_key, stats in container.items():
                if str(raw_key).strip().lower() not in strategy_keys:
                    continue
                if isinstance(stats, dict):
                    out.extend([
                        stats.get("recent_win_rate"),
                        stats.get("rolling_win_rate"),
                        stats.get("realized_win_rate"),
                        stats.get("realised_win_rate"),
                        stats.get("win_rate"),
                    ])
                else:
                    out.append(stats)
            return out

        if isinstance(portfolio, dict):
            meta = portfolio.get("metadata")
            if isinstance(meta, dict):
                dyn = meta.get("dynamic_thresholds")
                if isinstance(dyn, dict):
                    candidates.extend(_strategy_candidates(dyn.get("per_strategy")))
                candidates.extend(_strategy_candidates(meta.get("per_strategy")))
                candidates.extend(_strategy_candidates(meta.get("strategy_win_rates")))
                candidates.extend([
                    meta.get("recent_win_rate"),
                    meta.get("rolling_win_rate"),
                    meta.get("realized_win_rate"),
                    meta.get("realised_win_rate"),
                ])
            dyn = portfolio.get("dynamic_thresholds")
            if isinstance(dyn, dict):
                candidates.extend(_strategy_candidates(dyn.get("per_strategy")))
            candidates.extend(_strategy_candidates(portfolio.get("per_strategy")))
            candidates.extend(_strategy_candidates(portfolio.get("strategy_win_rates")))
            candidates.extend([
                portfolio.get("recent_win_rate"),
                portfolio.get("rolling_win_rate"),
                portfolio.get("realized_win_rate"),
                portfolio.get("realised_win_rate"),
            ])
        if signal is not None and isinstance(getattr(signal, "metadata", None), dict):
            dyn = signal.metadata.get("dynamic_thresholds")
            if isinstance(dyn, dict):
                candidates.extend(_strategy_candidates(dyn.get("per_strategy")))
            candidates.extend(_strategy_candidates(signal.metadata.get("per_strategy")))
            candidates.extend(_strategy_candidates(signal.metadata.get("strategy_win_rates")))
            candidates.extend([
                signal.metadata.get("recent_win_rate"),
                signal.metadata.get("rolling_win_rate"),
                signal.metadata.get("realized_win_rate"),
                signal.metadata.get("realised_win_rate"),
            ])
        for raw in candidates:
            if raw is None:
                continue
            try:
                v = float(raw)
            except (TypeError, ValueError):
                continue
            if v > 1.0 and v <= 100.0:
                v = v / 100.0
            return max(0.0, min(1.0, v))
        return None

    def _market_state_score(self, portfolio: dict, signal: Signal | None = None, *, default: float = 1.0) -> float:
        raw = None
        if isinstance(portfolio, dict):
            meta = portfolio.get("metadata", {})
            if isinstance(meta, dict):
                raw = meta.get("market_state_score")
        if signal is not None and isinstance(getattr(signal, "metadata", None), dict):
            raw = signal.metadata.get("market_state_score", raw)
        try:
            v = float(raw) if raw is not None else float(default)
        except (TypeError, ValueError):
            v = float(default)
        return max(0.0, min(1.0, v))

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _reject(self, signal, reason, passed, failed) -> RiskDecision:
        return RiskDecision(
            verdict=RiskVerdict.REJECTED,
            reason=reason,
            signal_id=signal.signal_id,
            checks_passed=passed,
            checks_failed=failed,
        )

    @staticmethod
    def _decimal_from_portfolio(portfolio: dict, key: str, default: Decimal) -> Decimal:
        try:
            return Decimal(str(portfolio.get(key, default)))
        except Exception:  # noqa: BLE001
            return default

    def _sizing_nav(self, portfolio: dict) -> Decimal:
        """
        Notional limits (% of portfolio, exposure caps, etc.) apply to the **tradable**
        sleeve (allocation slider), not necessarily full account equity.
        Falls back to portfolio_value when tradable_capital is absent.
        """
        t = portfolio.get("tradable_capital")
        if t is not None:
            try:
                d = Decimal(str(t))
                if d > 0:
                    return d
            except Exception:  # noqa: BLE001
                pass
        return self._decimal_from_portfolio(portfolio, "portfolio_value", Decimal("0"))

    @staticmethod
    def _resolve_signal_price(signal: Signal) -> Decimal:
        if signal.suggested_price is not None and signal.suggested_price > 0:
            return signal.suggested_price
        metadata = signal.metadata if isinstance(signal.metadata, dict) else {}
        for key in ("close", "last_price", "price"):
            if key not in metadata:
                continue
            try:
                price = Decimal(str(metadata[key]))
            except Exception:  # noqa: BLE001
                continue
            if price > 0:
                return price
        return Decimal("0")

    def _requested_notional(self, signal: Signal) -> Decimal:
        meta = signal.metadata if isinstance(getattr(signal, "metadata", None), dict) else {}
        override = meta.get("risk_notional_override")
        if override is not None:
            try:
                d = Decimal(str(override))
                if d >= 0:
                    return d
            except Exception:  # noqa: BLE001
                pass
        spec = parse_option_contract_from_metadata(meta)
        if spec is not None:
            px = self._resolve_signal_price(signal)
            if px > 0:
                return option_premium_notional(
                    abs(signal.suggested_quantity),
                    px,
                    int(spec.multiplier),
                )
        return abs(signal.suggested_quantity) * self._resolve_signal_price(signal)

    @staticmethod
    def _is_arbitrage_signal(signal: Signal) -> bool:
        return (getattr(signal, "side", "") or "").strip().upper().startswith("ARBITRAGE_")

    def _check_arbitrage_bundle(self, signal: Signal, portfolio: dict) -> tuple[bool, str]:
        if not self._is_arbitrage_signal(signal):
            return (True, "arbitrage_skipped")

        arb_cfg = self.config.get("arbitrage")
        if not isinstance(arb_cfg, dict) or not arb_cfg.get("enabled", False):
            return (True, "arbitrage_checks_disabled")
            
        # D120: Dynamic arbitrage exposure based on market state
        market_state_score = Decimal("1.0")
        if isinstance(portfolio, dict):
            pmeta = portfolio.get("metadata", {})
            if isinstance(pmeta, dict) and "market_state_score" in pmeta:
                try:
                    market_state_score = Decimal(str(pmeta["market_state_score"]))
                except (TypeError, ValueError, InvalidOperation):
                    pass
        
        # Clone the config to avoid mutating global state
        arb_cfg = dict(arb_cfg)
        if "max_total_arbitrage_exposure" in arb_cfg:
            try:
                base_exp = Decimal(str(arb_cfg["max_total_arbitrage_exposure"]))
                arb_cfg["max_total_arbitrage_exposure"] = str(base_exp * max(Decimal("0.1"), min(Decimal("1.0"), market_state_score)))
            except (TypeError, ValueError, InvalidOperation):
                pass

        side_u = (signal.side or "").upper()
        conc_raw = portfolio.get("venue_concentration", {})
        conc: dict = {}
        if isinstance(conc_raw, dict):
            from decimal import Decimal as D

            for k, v in conc_raw.items():
                try:
                    conc[str(k).strip().lower()] = D(str(v))
                except Exception:  # noqa: BLE001
                    pass

        from risk.arbitrage_checks import ArbitrageRiskChecks, ArbitrageVenueState
        from risk.cross_exchange_checks import CrossExchangeRiskChecks

        venue_state = ArbitrageVenueState(concentrations=conc)

        if "SPOT_SPREAD" in side_u:
            ccfg = arb_cfg.get("cross_spot") if isinstance(arb_cfg.get("cross_spot"), dict) else arb_cfg
            sig_d = {
                "symbol": signal.symbol,
                "buy_venue": (signal.metadata or {}).get("buy_venue", ""),
                "sell_venue": (signal.metadata or {}).get("sell_venue", ""),
                "metadata": signal.metadata or {},
            }
            ok, reason = CrossExchangeRiskChecks(ccfg).validate(sig_d, portfolio, venue_state)
            return (ok, f"cross_exchange_{reason}")

        ok, reason = ArbitrageRiskChecks(arb_cfg).validate_funding_signal(signal, portfolio, venue_state)
        return (ok, f"arbitrage_{reason}")

    def _infer_expected_loss_pct(self, signal: Signal) -> Optional[Decimal]:
        metadata = signal.metadata if isinstance(signal.metadata, dict) else {}
        for key in ("stop_loss_pct", "expected_loss_pct"):
            if key not in metadata:
                continue
            try:
                v = Decimal(str(metadata[key]))
            except Exception:  # noqa: BLE001
                continue
            if v > 0:
                return v

        price = self._resolve_signal_price(signal)
        if price <= 0:
            return None
        for key in ("atr_14", "atr"):
            if key not in metadata:
                continue
            try:
                atr = Decimal(str(metadata[key]))
            except Exception:  # noqa: BLE001
                continue
            if atr > 0:
                return atr / price
        return None

    def _minimum_order_size(self, asset_class: str) -> Decimal:
        asset = (asset_class or "").strip().lower()
        symbol = ""
        # symbol-aware key is set by execution engine when available.
        # pull from provider live/derived/default layers first.
        try:
            symbol = str(getattr(self, "_last_signal_symbol", "")).strip().upper()
        except Exception:  # noqa: BLE001
            symbol = ""
        if symbol:
            v = self._provider.get_decimal(
                f"minimum_order_size.symbol.{symbol}",
                fallback=Decimal("-1"),
            )
            if v >= 0:
                return v
        if asset:
            v = self._provider.get_decimal(
                f"minimum_order_size.asset_class.{asset}",
                fallback=Decimal("-1"),
            )
            if v >= 0:
                return v
        minimums = self.config.get("minimum_order_sizes_gbp", {})
        return Decimal(str(minimums.get(asset, 0)))

    def _effective_max_position_pct(self) -> Decimal:
        param_cap = self._provider.get_decimal("max_single_position_pct", fallback=Decimal("1.0"))
        cfg_cap = self._cfg_decimal("max_position_pct", fallback=Decimal("1.0"))
        return min(param_cap, cfg_cap)

    def _cfg_decimal(self, key: str, fallback: Decimal = Decimal("0")) -> Decimal:
        if key in self.config:
            try:
                return Decimal(str(self.config[key]))
            except Exception:  # noqa: BLE001
                return fallback
        return fallback

    async def persist_decision(self, session_factory, signal: Signal, decision: RiskDecision) -> None:
        """Persist every risk decision for audit; no-op if DB/session factory is missing."""
        if session_factory is None:
            return
        try:
            from storage.models import RiskLog
        except Exception as exc:  # noqa: BLE001
            logger.warning("Risk decision not persisted; failed to import RiskLog: %s", exc)
            return

        ts = datetime.now(timezone.utc)
        try:
            raw_ts = getattr(signal, "timestamp", None)
            if isinstance(raw_ts, str):
                ts = datetime.fromisoformat(raw_ts.replace("Z", "+00:00"))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
        except Exception:  # noqa: BLE001
            pass

        row = RiskLog(
            signal_id=decision.signal_id[:128],
            timestamp=ts,
            verdict=decision.verdict.value[:10],
            reason=decision.reason,
            checks_passed=decision.checks_passed,
            checks_failed=decision.checks_failed,
        )
        try:
            async with session_factory() as session:
                session.add(row)
                await session.commit()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Risk decision persistence failed | signal_id=%s | %s", decision.signal_id, exc)

    def set_live_parameter(self, key: str, value: Decimal | str | float | int) -> None:
        """Allow execution/runtime services to inject fresh live parameter values."""
        self._provider.set_live(key, value)
