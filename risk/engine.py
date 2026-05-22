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
        self._is_killed = False
        self._disabled_brokers: set[str] = set()
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

        # D125 fix #5 — record the approved open notional against the
        # per-UTC-day cumulative-add tracker so the next signal on the
        # same symbol can see this one even before it's filled.
        # Reduce-only is skipped (it lowers exposure, not adds it).
        if not self._is_reduce_only_signal(signal):
            try:
                proposed = self._requested_notional(signal)
                if proposed > 0:
                    self.record_open_signal_notional(str(signal.symbol or ""), proposed)
            except Exception:  # noqa: BLE001 — recording is non-fatal
                pass

        logger.info(f"RISK APPROVED {signal.signal_id} | {signal.symbol} {signal.side}")
        return RiskDecision(
            verdict=RiskVerdict.APPROVED,
            reason="All checks passed",
            signal_id=signal.signal_id,
            checks_passed=checks_passed,
            checks_failed=[],
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
        self._persist_runtime_state()
        logger.warning("Kill switch deactivated")

    def disable_broker(self, name: str) -> None:
        """Stop new orders routed to this broker (execution auto-fail / targeted control)."""
        n = (name or "").strip().lower()
        if not n:
            return
        self._disabled_brokers.add(n)
        self._persist_runtime_state()
        logger.critical("RISK | broker disabled for new orders | broker=%s", n)

    def enable_broker(self, name: str) -> None:
        n = (name or "").strip().lower()
        if n:
            self._disabled_brokers.discard(n)
            self._persist_runtime_state()
            logger.warning("RISK | broker re-enabled | broker=%s", n)

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
        if bool(portfolio_state.get("is_killed")):
            self._is_killed = True
        raw_disabled = portfolio_state.get("disabled_brokers")
        if isinstance(raw_disabled, (list, tuple, set)):
            self._disabled_brokers.update({
                str(name).strip().lower()
                for name in raw_disabled
                if str(name).strip()
            })
        self._persist_runtime_state()

    def snapshot_runtime_state(self) -> dict:
        return {
            "consecutive_losses": int(self._consecutive_losses),
            "daily_loss_accumulated": str(self._daily_loss),
            "cooldown_until": self._cooldown_until.isoformat() if self._cooldown_until else None,
            "is_killed": bool(self._is_killed),
            "disabled_brokers": sorted(self._disabled_brokers),
        }

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
        
        market_state_score = Decimal("1.0")
        if isinstance(portfolio, dict):
            pmeta = portfolio.get("metadata", {})
            if isinstance(pmeta, dict) and "market_state_score" in pmeta:
                try:
                    market_state_score = Decimal(str(pmeta["market_state_score"]))
                except (TypeError, ValueError, InvalidOperation):
                    pass
        
        max_drawdown_pct = base_max_drawdown_pct * max(Decimal("0.1"), min(Decimal("1.0"), market_state_score))
        drawdown = (self._high_watermark - portfolio_value) / self._high_watermark
        return (drawdown <= max_drawdown_pct, "drawdown_limit")

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

        # D120 Dynamic FX Cap (0 to 100%)
        # No fixed numbers. Cap is 1:1 with market_state_score.
        market_state_score = Decimal("0.15") # Fallback
        if isinstance(portfolio, dict):
            metadata = portfolio.get("metadata", {})
            if isinstance(metadata, dict):
                mss = metadata.get("market_state_score")
                if mss is not None:
                    try:
                        market_state_score = Decimal(str(mss))
                    except (TypeError, ValueError, InvalidOperation):
                        pass

        if getattr(signal, "metadata", None):
            mss = signal.metadata.get("market_state_score")
            if mss is not None:
                try:
                    market_state_score = Decimal(str(mss))
                except (TypeError, ValueError, InvalidOperation):
                    pass

        # Fully dynamic: 0.0 (0%) to 1.0 (100%)
        max_pct = max(Decimal("0.0"), min(Decimal("1.0"), market_state_score))

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

        # D120 Dynamic Equity Cluster Cap (0 to 100%)
        # Cap is 1:1 with market_state_score.
        market_state_score = Decimal("0.20") # Fallback
        if isinstance(portfolio, dict):
            metadata = portfolio.get("metadata", {})
            if isinstance(metadata, dict):
                mss = metadata.get("market_state_score")
                if mss is not None:
                    try:
                        market_state_score = Decimal(str(mss))
                    except (TypeError, ValueError, InvalidOperation):
                        pass

        if getattr(signal, "metadata", None):
            mss = signal.metadata.get("market_state_score")
            if mss is not None:
                try:
                    market_state_score = Decimal(str(mss))
                except (TypeError, ValueError, InvalidOperation):
                    pass

        # Fully dynamic: 0.0 (0%) to 1.0 (100%)
        max_pct = max(Decimal("0.0"), min(Decimal("1.0"), market_state_score))
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

    @staticmethod
    def _normalize_symbol_list(raw) -> set[str]:
        out: set[str] = set()
        if isinstance(raw, (list, tuple, set)):
            for v in raw:
                s = str(v or "").strip().upper()
                if s:
                    out.add(s)
        return out

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

        # Existing position notional on the same symbol (signed magnitude
        # — we compare |projected| against the cap, so direction-agnostic
        # for net new exposure).
        existing = Decimal("0")
        positions = portfolio.get("positions", {}) if isinstance(portfolio, dict) else {}
        if isinstance(positions, dict):
            for pos_key, pos in positions.items():
                if not isinstance(pos, dict):
                    continue
                # Position keys are sometimes "broker:SYMBOL" or just "SYMBOL".
                k_sym = str(pos.get("symbol") or pos_key).split(":", 1)[-1].strip().upper()
                if k_sym != signal_sym:
                    continue
                try:
                    qty = Decimal(str(pos.get("quantity", "0") or "0"))
                except (InvalidOperation, TypeError, ValueError):
                    continue
                if qty == 0:
                    continue
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
                    continue
                existing += abs(qty) * price

        projected = existing + proposed
        if projected <= cap_notional:
            return (True, "single_name_notional")

        logger.warning(
            "RISK single_name_notional REJECT | %s | existing=%s + proposed=%s = %s > cap=%s (%.2f%% of NAV %s)",
            signal_sym,
            str(existing),
            str(proposed),
            str(projected),
            str(cap_notional),
            float(max_pct * Decimal("100")),
            str(sizing_base),
        )
        return (False, "single_name_notional")

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

        self._roll_intraday_adds_day_if_needed()
        already = self._intraday_added_notional.get(signal_sym, Decimal("0"))
        projected = already + proposed
        if projected <= cap_notional:
            return (True, "intraday_symbol_adds")

        logger.warning(
            "RISK intraday_symbol_adds REJECT | %s | added_today=%s + proposed=%s = %s > cap=%s (%.2f%% of NAV %s)",
            signal_sym,
            str(already),
            str(proposed),
            str(projected),
            str(cap_notional),
            float(max_pct * Decimal("100")),
            str(sizing_base),
        )
        return (False, "intraday_symbol_adds")

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
        base_min_confidence = float(self.config.get("min_signal_confidence", 1.0))
        
        market_state_score = 1.0
        if isinstance(portfolio, dict):
            pmeta = portfolio.get("metadata", {})
            if isinstance(pmeta, dict) and "market_state_score" in pmeta:
                try:
                    market_state_score = float(pmeta["market_state_score"])
                except (TypeError, ValueError):
                    pass
        
        # Scale inversely: in terrible market (0.0), threshold raises up to 2x base.
        min_confidence = base_min_confidence * (2.0 - max(0.1, min(1.0, market_state_score)))
        
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
        if bool(meta.get("sizing_topup_existing")) and str(meta.get("coordinator_kind", "")).lower() == "open_strategy":
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

        market_state_score = 1.0
        if isinstance(portfolio, dict):
            pmeta = portfolio.get("metadata", {})
            if isinstance(pmeta, dict) and "market_state_score" in pmeta:
                try:
                    market_state_score = float(pmeta["market_state_score"])
                except (TypeError, ValueError):
                    pass
        
        threshold = base_threshold * (2.0 - max(0.1, min(1.0, market_state_score)))

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
        except Exception:  # noqa: BLE001
            pass

        passed = quality >= threshold
        if not passed:
            logger.debug(
                "RISK quality_gate | {} | conf={:.2f} news={:.2f} vol_z={:.2f} → quality={:.3f} < threshold={:.2f}",
                signal.symbol, confidence, abs(news_raw), volume_z, quality, threshold,
            )
        return (passed, "trade_quality")

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
