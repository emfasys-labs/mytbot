"""Drawdown-state open governor.

When intraday de-risking reaches a serious tier, the system should stop
opening fresh risk for a cooling-off period. Reduce-only exits, profit
harvests, and hedges remain allowed.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping


@dataclass(frozen=True)
class DrawdownOpenLockConfig:
    enabled: bool = True
    trigger_tier_idx: int = 1
    cooldown_sec: float = 900.0
    recover_hysteresis_pct: Decimal = Decimal("0.0015")


def parse_open_lock_config(raw: Mapping[str, Any] | None) -> DrawdownOpenLockConfig:
    if not isinstance(raw, Mapping):
        return DrawdownOpenLockConfig()
    try:
        tier = int(raw.get("trigger_tier_idx", 1))
    except (TypeError, ValueError):
        tier = 1
    try:
        cooldown = float(raw.get("cooldown_sec", 900.0))
    except (TypeError, ValueError):
        cooldown = 900.0
    try:
        hyst = Decimal(str(raw.get("recover_hysteresis_pct", "0.0015")))
    except (InvalidOperation, TypeError, ValueError):
        hyst = Decimal("0.0015")
    return DrawdownOpenLockConfig(
        enabled=bool(raw.get("enabled", True)),
        trigger_tier_idx=max(0, tier),
        cooldown_sec=max(0.0, cooldown),
        recover_hysteresis_pct=max(Decimal("0"), hyst),
    )


def should_trigger_open_lock(*, tier_idx: int, config: DrawdownOpenLockConfig) -> bool:
    return bool(config.enabled and tier_idx >= config.trigger_tier_idx and config.cooldown_sec > 0)


def derisk_execution_reduced_exposure(result: Any) -> bool:
    """Return true only when a derisk order actually filled exposure reduction.

    The open-lock is a post-derisk cooldown, not a standing drawdown-state
    switch. Tying it to filled reduce-only execution prevents an old realised
    loss from refreshing the lock forever when no new derisk work succeeded.
    """
    if result is None:
        return False
    status = getattr(result, "status", None)
    status_value = str(getattr(status, "value", status) or "").strip().lower()
    if status_value not in {"filled", "partially_filled"}:
        return False
    try:
        filled = Decimal(str(getattr(result, "filled_quantity", "0") or "0"))
    except (InvalidOperation, TypeError, ValueError):
        return False
    return filled > 0


def recovered_from_tier(
    *,
    nav: Decimal,
    day_pnl: Decimal,
    tier_threshold_pct: Decimal,
    config: DrawdownOpenLockConfig,
) -> bool:
    if not config.enabled or nav <= 0:
        return False
    recover_pct = tier_threshold_pct + config.recover_hysteresis_pct
    return (day_pnl / nav) >= recover_pct
