"""
core/session_exit_policy.py
===========================
Session-aware pre-close position policy.

The execution gate remains binary physics ("a closed venue cannot fill").
This module is the decision layer: it turns the approaching close into a
graded position action, preserving multi-day theses while closing/ trimming
positions whose own profile says they should not be carried overnight.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from core.market_session import minutes_to_session_close

SessionExitAction = Literal[
    "hold_through_close",
    "trim_before_close",
    "close_before_close",
    "defer_action",
]


@dataclass(frozen=True)
class SessionExitDecision:
    action: SessionExitAction
    reason: str
    minutes_to_close: float | None
    reduce_fraction: Decimal = Decimal("0")
    should_submit_order: bool = False


def _d(raw: Any, default: str = "0") -> Decimal:
    try:
        return Decimal(str(raw))
    except Exception:  # noqa: BLE001
        return Decimal(default)


def load_session_exit_policy(path: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    """Best-effort YAML load with safe defaults."""
    p = Path(path or os.getenv("SESSION_EXIT_POLICY_CONFIG", "config/session_exit_policy.yaml"))
    cfg: dict[str, Any] = {}
    try:
        if p.is_file():
            import yaml

            loaded = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            if isinstance(loaded, dict):
                cfg = loaded
    except Exception:  # noqa: BLE001
        cfg = {}
    return {
        "enabled": True,
        "windows": {
            "warning_minutes_before_close": 60,
            "decision_minutes_before_close": 20,
            "hard_exit_minutes_before_close": 5,
        },
        "defaults": {
            "holding_horizon": "swing",
            "allow_overnight": True,
            "allow_weekend_hold": False,
            "max_loss_pct_for_hold": "-0.015",
            "min_profit_pct_for_defender_trim": "0.004",
        },
        "horizons": {
            "scalp": {"allow_overnight": False, "action": "close_before_close"},
            "intraday": {"allow_overnight": False, "action": "close_before_close"},
            "swing": {"allow_overnight": True, "action": "hold_through_close"},
            "position": {"allow_overnight": True, "action": "hold_through_close"},
        },
        "modes": {
            "defender": {
                "trim_fraction": "0.50",
                "trim_profitable_swing_before_close": True,
                "close_losing_intraday_in_warning_window": True,
            },
            "trader": {
                "trim_fraction": "0.25",
                "trim_profitable_swing_before_close": False,
                "close_losing_intraday_in_warning_window": True,
            },
            "hunter": {
                "trim_fraction": "0",
                "trim_profitable_swing_before_close": False,
                "close_losing_intraday_in_warning_window": False,
            },
        },
        **cfg,
    }


def _metadata_bool(md: dict[str, Any], key: str) -> bool | None:
    if key not in md:
        return None
    v = md.get(key)
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if s in {"1", "true", "yes", "on"}:
        return True
    if s in {"0", "false", "no", "off"}:
        return False
    return None


def _resolve_horizon(strategy_name: str | None, metadata: dict[str, Any], cfg: dict[str, Any]) -> str:
    for key in ("holding_horizon", "strategy_horizon", "session_horizon"):
        val = metadata.get(key)
        if val:
            h = str(val).strip().lower()
            if h:
                return h
    strat = str(strategy_name or "").strip().lower()
    if any(tok in strat for tok in ("scalp", "market_maker", "micro")):
        return "scalp"
    if any(tok in strat for tok in ("intraday", "day_trade")):
        return "intraday"
    if any(tok in strat for tok in ("regime_rotation", "factor", "position")):
        return "position"
    return str(((cfg.get("defaults") or {}).get("holding_horizon") or "swing")).strip().lower()


def _unrealised_return(quantity: Decimal, avg_entry_price: Decimal, current_price: Decimal) -> Decimal:
    if avg_entry_price <= 0 or current_price <= 0 or quantity == 0:
        return Decimal("0")
    raw = (current_price - avg_entry_price) / avg_entry_price
    return raw if quantity > 0 else -raw


def evaluate_session_exit(
    *,
    broker: str,
    asset_class: str,
    symbol: str,
    quantity: Decimal,
    avg_entry_price: Decimal,
    current_price: Decimal,
    strategy_name: str | None = None,
    profile_mode: str = "trader",
    metadata: dict[str, Any] | None = None,
    now: datetime | None = None,
    config: dict[str, Any] | None = None,
) -> SessionExitDecision:
    """Decide whether a position should be carried, trimmed, or closed.

    Defaults are deliberately hold-friendly: only explicit short-horizon or
    overnight-disabled positions are closed automatically before the session
    ends. Risk stops still live in the risk/stop monitor and can override this.
    """
    cfg = config or load_session_exit_policy()
    if not bool(cfg.get("enabled", True)):
        return SessionExitDecision("hold_through_close", "session_exit_policy_disabled", None)

    now = now or datetime.now(timezone.utc)
    md = dict(metadata or {})
    mtc = minutes_to_session_close(broker, asset_class, symbol, now)
    if mtc is None:
        return SessionExitDecision("hold_through_close", "no_finite_session_close", None)

    windows = cfg.get("windows") or {}
    warning = float(windows.get("warning_minutes_before_close", 60) or 60)
    decision = float(windows.get("decision_minutes_before_close", 20) or 20)
    hard = float(windows.get("hard_exit_minutes_before_close", 5) or 5)
    if mtc > warning:
        return SessionExitDecision("hold_through_close", "outside_pre_close_window", mtc)

    mode = str(profile_mode or "trader").strip().lower()
    mode_cfg = (cfg.get("modes") or {}).get(mode) or (cfg.get("modes") or {}).get("trader") or {}
    horizon = _resolve_horizon(strategy_name, md, cfg)
    horizon_cfg = (cfg.get("horizons") or {}).get(horizon, {})
    defaults = cfg.get("defaults") or {}

    allow_overnight = _metadata_bool(md, "allow_overnight")
    if allow_overnight is None:
        allow_overnight = bool(horizon_cfg.get("allow_overnight", defaults.get("allow_overnight", True)))

    unrl = _unrealised_return(quantity, avg_entry_price, current_price)
    max_loss_for_hold = _d(defaults.get("max_loss_pct_for_hold", "-0.015"), "-0.015")
    min_profit_for_trim = _d(defaults.get("min_profit_pct_for_defender_trim", "0.004"), "0.004")
    trim_fraction = max(Decimal("0"), min(Decimal("1"), _d(mode_cfg.get("trim_fraction", "0"), "0")))

    if not allow_overnight:
        if mtc <= decision or mtc <= hard:
            return SessionExitDecision(
                "close_before_close",
                f"{horizon}_overnight_not_allowed",
                mtc,
                reduce_fraction=Decimal("1"),
                should_submit_order=True,
            )
        if unrl < 0 and bool(mode_cfg.get("close_losing_intraday_in_warning_window", True)):
            return SessionExitDecision(
                "close_before_close",
                f"{horizon}_losing_before_close",
                mtc,
                reduce_fraction=Decimal("1"),
                should_submit_order=True,
            )
        return SessionExitDecision("defer_action", f"{horizon}_pending_pre_close_review", mtc)

    if unrl <= max_loss_for_hold and mtc <= decision:
        # This is not a stop-loss replacement; it prevents carrying a weak
        # short-horizon/ambiguous thesis through a closed risk-management gap.
        return SessionExitDecision(
            "trim_before_close",
            "overnight_allowed_but_loss_near_close",
            mtc,
            reduce_fraction=trim_fraction or Decimal("0.25"),
            should_submit_order=True,
        )

    if (
        mode == "defender"
        and trim_fraction > 0
        and unrl >= min_profit_for_trim
        and bool(mode_cfg.get("trim_profitable_swing_before_close", True))
        and mtc <= decision
    ):
        return SessionExitDecision(
            "trim_before_close",
            "defender_bank_profit_before_close",
            mtc,
            reduce_fraction=trim_fraction,
            should_submit_order=True,
        )

    return SessionExitDecision("hold_through_close", f"{horizon}_overnight_allowed", mtc)
