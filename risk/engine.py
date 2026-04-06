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
from decimal import Decimal
from enum import Enum
from typing import Optional
import logging

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
        self._daily_loss = Decimal("0")
        self._consecutive_losses = 0
        self._in_cooldown = False
        self._is_killed = False
        self._high_watermark = Decimal("0")

    # ── Public API ────────────────────────────────────────────────────────────

    def evaluate(self, signal: Signal, portfolio_state: dict) -> RiskDecision:
        """
        Run all risk checks on a signal.
        Returns a RiskDecision with APPROVED or REJECTED verdict.
        """

        if self._is_killed:
            return self._reject(signal, "KILL SWITCH ACTIVE", [], ["kill_switch"])

        checks_passed = []
        checks_failed = []

        checks = [
            self._check_kill_switch,
            self._check_cooldown,
            self._check_daily_loss_limit,
            self._check_drawdown_limit,
            self._check_position_size,
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
        self._in_cooldown = False

    def update_high_watermark(self, portfolio_value: Decimal) -> None:
        """Track best observed portfolio value for drawdown checks."""
        if portfolio_value > self._high_watermark:
            self._high_watermark = portfolio_value

    # ── Checks ────────────────────────────────────────────────────────────────

    def _check_kill_switch(self, signal, portfolio) -> tuple[bool, str]:
        return (not self._is_killed, "kill_switch")

    def _check_cooldown(self, signal, portfolio) -> tuple[bool, str]:
        return (not self._in_cooldown, "cooldown")

    def _check_daily_loss_limit(self, signal, portfolio) -> tuple[bool, str]:
        portfolio_value = self._decimal_from_portfolio(portfolio, "portfolio_value", Decimal("0"))
        if portfolio_value <= 0:
            return (False, "daily_loss_limit")
        max_daily_loss_pct = Decimal(str(self.config.get("max_daily_loss_pct", 0.02)))
        # Use whichever loss tracker is worse: runtime or provided portfolio state.
        stated_pnl = self._decimal_from_portfolio(portfolio, "daily_realized_pnl", Decimal("0"))
        state_loss = abs(stated_pnl) if stated_pnl < 0 else Decimal("0")
        observed_loss = max(self._daily_loss, state_loss)
        allowed_loss = portfolio_value * max_daily_loss_pct
        return (observed_loss <= allowed_loss, "daily_loss_limit")

    def _check_position_size(self, signal, portfolio) -> tuple[bool, str]:
        portfolio_value = self._decimal_from_portfolio(portfolio, "portfolio_value", Decimal("0"))
        if portfolio_value <= 0:
            return (False, "position_size")
        max_position_pct = Decimal(str(self.config.get("max_position_pct", 0.10)))
        price = self._resolve_signal_price(signal)
        if price <= 0:
            return (False, "position_size")
        requested_notional = abs(signal.suggested_quantity) * price
        allowed_notional = portfolio_value * max_position_pct
        return (requested_notional <= allowed_notional, "position_size")

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
        max_drawdown_pct = Decimal(str(self.config.get("max_drawdown_pct", 0.10)))
        drawdown = (self._high_watermark - portfolio_value) / self._high_watermark
        return (drawdown <= max_drawdown_pct, "drawdown_limit")

    def _check_max_exposure(self, signal, portfolio) -> tuple[bool, str]:
        portfolio_value = self._decimal_from_portfolio(portfolio, "portfolio_value", Decimal("0"))
        if portfolio_value <= 0:
            return (False, "max_exposure")
        max_gross_pct = Decimal(str(self.config.get("max_gross_exposure_pct", 0.80)))
        current_gross = self._decimal_from_portfolio(portfolio, "current_gross_exposure", Decimal("0"))
        projected_gross = current_gross + self._requested_notional(signal)
        allowed_gross = portfolio_value * max_gross_pct
        return (projected_gross <= allowed_gross, "max_exposure")

    def _check_concentration(self, signal, portfolio) -> tuple[bool, str]:
        portfolio_value = self._decimal_from_portfolio(portfolio, "portfolio_value", Decimal("0"))
        if portfolio_value <= 0:
            return (False, "concentration")
        max_concentration_pct = Decimal(str(self.config.get("max_concentration_pct", 0.20)))
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
            return (True, "asset_class_limit")

        pct = Decimal(str(self.config.get(config_key, "1.0")))
        allowed = portfolio_value * pct
        return (projected <= allowed, "asset_class_limit")

    def _check_consecutive_losses(self, signal, portfolio) -> tuple[bool, str]:
        max_losses = self.config.get("max_consecutive_losses", 3)
        if self._consecutive_losses >= max_losses:
            self._in_cooldown = True
            return (False, "consecutive_losses")
        return (True, "consecutive_losses")

    def _check_confidence_threshold(self, signal, portfolio) -> tuple[bool, str]:
        min_confidence = self.config.get("min_signal_confidence", 0.5)
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
