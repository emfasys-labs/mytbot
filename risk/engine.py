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
from decimal import Decimal
from enum import Enum
from typing import Optional
import logging

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
        self._high_watermark = Decimal("0")

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
            self._check_cooldown,
            self._check_asset_proportionality,
            self._check_minimum_order_size,
            self._check_daily_loss_limit,
            self._check_max_trades_per_day,
            self._check_drawdown_limit,
            self._check_position_size,
            self._check_max_loss_per_trade_pct,
            self._check_max_exposure,
            self._check_concentration,
            self._check_asset_class_limits,
            self._check_consecutive_losses,
            self._check_confidence_threshold,
        ]

        for check in checks:
            result, label = check(signal, portfolio_state)
            if result:
                checks_passed.append(label)
            else:
                checks_failed.append(label)
                decision = self._reject(signal, f"Failed: {label}", checks_passed, checks_failed)
                logger.warning(f"RISK REJECTED {signal.signal_id} | {label}")
                return decision

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
        logger.critical("KILL SWITCH ACTIVATED — no new orders will be placed")

    def reset_kill(self) -> None:
        """Deactivate kill switch. Must be deliberate manual action."""
        self._is_killed = False
        logger.warning("Kill switch deactivated")

    @property
    def is_killed(self) -> bool:
        return self._is_killed

    def record_loss(self, amount: Decimal) -> None:
        """Called by execution engine after a losing trade."""
        self._daily_loss += amount
        self._consecutive_losses += 1

    def record_win(self) -> None:
        """Called by execution engine after a winning trade."""
        self._consecutive_losses = 0

    def reset_daily(self) -> None:
        """Called at start of each trading day."""
        self._daily_loss = Decimal("0")
        self._cooldown_until = None

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

    def snapshot_runtime_state(self) -> dict:
        return {
            "consecutive_losses": int(self._consecutive_losses),
            "daily_loss_accumulated": self._daily_loss,
            "cooldown_until": self._cooldown_until.isoformat() if self._cooldown_until else None,
        }

    # ── Checks ────────────────────────────────────────────────────────────────

    def _check_kill_switch(self, signal, portfolio) -> tuple[bool, str]:
        return (not self._is_killed, "kill_switch")

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
        max_daily_loss_pct = self._provider.get_decimal("max_daily_loss_pct", fallback=Decimal("0"))
        # Use whichever loss tracker is worse: runtime or provided portfolio state.
        stated_pnl = self._decimal_from_portfolio(portfolio, "daily_realized_pnl", Decimal("0"))
        state_loss = abs(stated_pnl) if stated_pnl < 0 else Decimal("0")
        observed_loss = max(self._daily_loss, state_loss)
        allowed_loss = portfolio_value * max_daily_loss_pct
        return (observed_loss <= allowed_loss, "daily_loss_limit")

    def _check_max_trades_per_day(self, signal, portfolio) -> tuple[bool, str]:
        max_trades = int(self.config.get("max_trades_per_day", 0))
        if max_trades <= 0:
            return (True, "max_trades_per_day")
        trades_today = int(portfolio.get("trades_today", 0))
        return (trades_today < max_trades, "max_trades_per_day")

    def _check_position_size(self, signal, portfolio) -> tuple[bool, str]:
        portfolio_value = self._decimal_from_portfolio(portfolio, "portfolio_value", Decimal("0"))
        if portfolio_value <= 0:
            return (False, "position_size")
        max_position_pct = self._effective_max_position_pct()
        price = self._resolve_signal_price(signal)
        if price <= 0:
            return (False, "position_size")
        requested_notional = abs(signal.suggested_quantity) * price
        allowed_notional = portfolio_value * max_position_pct
        return (requested_notional <= allowed_notional, "position_size")

    def _check_asset_proportionality(self, signal, portfolio) -> tuple[bool, str]:
        portfolio_value = self._decimal_from_portfolio(portfolio, "portfolio_value", Decimal("0"))
        if portfolio_value <= 0:
            return (False, "asset_proportionality")
        threshold_pct = self._provider.get_decimal("proportionality_threshold_pct", fallback=Decimal("0"))
        minimum = self._minimum_order_size(signal.asset_class)
        if minimum <= 0:
            return (True, "asset_proportionality")
        return (minimum < (portfolio_value * threshold_pct), "asset_proportionality")

    def _check_minimum_order_size(self, signal, portfolio) -> tuple[bool, str]:
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
        max_drawdown_pct = self._provider.get_decimal("max_drawdown_pct", fallback=Decimal("0"))
        drawdown = (self._high_watermark - portfolio_value) / self._high_watermark
        return (drawdown <= max_drawdown_pct, "drawdown_limit")

    def _check_max_loss_per_trade_pct(self, signal, portfolio) -> tuple[bool, str]:
        portfolio_value = self._decimal_from_portfolio(portfolio, "portfolio_value", Decimal("0"))
        if portfolio_value <= 0:
            return (False, "max_loss_per_trade_pct")
        max_loss_pct = self._provider.get_decimal("max_loss_per_trade_pct", fallback=Decimal("0"))
        requested_notional = self._requested_notional(signal)
        expected_loss_pct = self._infer_expected_loss_pct(signal)
        if expected_loss_pct is None:
            # Keep the gate non-blocking when stop-distance proxy is unavailable.
            return (True, "max_loss_per_trade_pct")
        expected_loss = requested_notional * expected_loss_pct
        allowed_loss = portfolio_value * max_loss_pct
        return (expected_loss <= allowed_loss, "max_loss_per_trade_pct")

    def _check_max_exposure(self, signal, portfolio) -> tuple[bool, str]:
        portfolio_value = self._decimal_from_portfolio(portfolio, "portfolio_value", Decimal("0"))
        if portfolio_value <= 0:
            return (False, "max_exposure")
        max_gross_pct = self._provider.get_decimal("max_gross_exposure_pct", fallback=Decimal("0"))
        current_gross = self._decimal_from_portfolio(portfolio, "current_gross_exposure", Decimal("0"))
        projected_gross = current_gross + self._requested_notional(signal)
        allowed_gross = portfolio_value * max_gross_pct
        return (projected_gross <= allowed_gross, "max_exposure")

    def _check_concentration(self, signal, portfolio) -> tuple[bool, str]:
        portfolio_value = self._decimal_from_portfolio(portfolio, "portfolio_value", Decimal("0"))
        if portfolio_value <= 0:
            return (False, "concentration")
        max_concentration_pct = self._provider.get_decimal("max_concentration_pct", fallback=Decimal("0"))
        symbol_exposure_raw = portfolio.get("symbol_exposure", {})
        symbol_exposure = Decimal("0")
        if isinstance(symbol_exposure_raw, dict):
            symbol_exposure = Decimal(str(symbol_exposure_raw.get(signal.symbol, "0")))
        projected_symbol_exposure = symbol_exposure + self._requested_notional(signal)
        allowed_symbol_exposure = portfolio_value * max_concentration_pct
        return (projected_symbol_exposure <= allowed_symbol_exposure, "concentration")

    def _check_asset_class_limits(self, signal, portfolio) -> tuple[bool, str]:
        portfolio_value = self._decimal_from_portfolio(portfolio, "portfolio_value", Decimal("0"))
        if portfolio_value <= 0:
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
            "equity": "max_single_stock_pct",
        }
        config_key = key_by_class.get(asset_class)
        if config_key is None:
            logger.warning(
                "Asset class '%s' has no configured limit key; allowing by default",
                asset_class,
            )
            return (True, "asset_class_limit")

        pct = self._cfg_decimal(config_key, fallback=Decimal("1.0"))
        allowed = portfolio_value * pct
        return (projected <= allowed, "asset_class_limit")

    def _check_consecutive_losses(self, signal, portfolio) -> tuple[bool, str]:
        max_losses = int(self.config.get("max_consecutive_losses", 0))
        if self._consecutive_losses >= max_losses:
            cooldown_minutes = int(self.config.get("cooldown_minutes", 0))
            self._cooldown_until = datetime.now(timezone.utc) + timedelta(minutes=max(0, cooldown_minutes))
            return (False, "consecutive_losses")
        return (True, "consecutive_losses")

    def _check_confidence_threshold(self, signal, portfolio) -> tuple[bool, str]:
        min_confidence = float(self.config.get("min_signal_confidence", 1.0))
        return (signal.confidence >= min_confidence, "confidence_threshold")

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
        return abs(signal.suggested_quantity) * self._resolve_signal_price(signal)

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
