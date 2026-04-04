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
            self._check_position_size,
            self._check_max_exposure,
            self._check_concentration,
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

    def kill(self) -> None:
        """Activate kill switch. Halts all new orders immediately."""
        self._is_killed = True
        logger.critical("KILL SWITCH ACTIVATED — no new orders will be placed")

    def reset_kill(self) -> None:
        """Deactivate kill switch. Must be deliberate manual action."""
        self._is_killed = False
        logger.warning("Kill switch deactivated")

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

    # ── Checks ────────────────────────────────────────────────────────────────

    def _check_kill_switch(self, signal, portfolio) -> tuple[bool, str]:
        return (not self._is_killed, "kill_switch")

    def _check_cooldown(self, signal, portfolio) -> tuple[bool, str]:
        return (not self._in_cooldown, "cooldown")

    def _check_daily_loss_limit(self, signal, portfolio) -> tuple[bool, str]:
        # TODO M4: compare _daily_loss against config["max_daily_loss_pct"] * portfolio value
        return (True, "daily_loss_limit")

    def _check_position_size(self, signal, portfolio) -> tuple[bool, str]:
        # TODO M4: signal.suggested_quantity * price <= config["max_position_pct"] * portfolio value
        return (True, "position_size")

    def _check_max_exposure(self, signal, portfolio) -> tuple[bool, str]:
        # TODO M4: total open notional <= config["max_gross_exposure_pct"] * portfolio value
        return (True, "max_exposure")

    def _check_concentration(self, signal, portfolio) -> tuple[bool, str]:
        # TODO M4: single asset not > config["max_concentration_pct"] of portfolio
        return (True, "concentration")

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
