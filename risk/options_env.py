"""
risk/options_env.py
===================
Merge process environment into ``risk_limits.yaml`` ``options_trading`` block so
operators can toggle IBKR options without editing YAML.
"""

from __future__ import annotations

import os
from decimal import Decimal
from typing import Any


def _parse_underlyings(raw: str) -> list[str]:
    parts = [p.strip().upper() for p in raw.replace(";", ",").split(",")]
    return [p for p in parts if p]


def merge_options_env_into_risk_cfg(cfg: dict[str, Any]) -> None:
    """Mutate *cfg* in place (idempotent-safe reads)."""
    ot = cfg.get("options_trading")
    if not isinstance(ot, dict):
        ot = {}
        cfg["options_trading"] = ot

    if "ENABLE_OPTIONS" in os.environ:
        ot["enabled"] = os.getenv("ENABLE_OPTIONS", "").strip().lower() in ("1", "true", "yes", "on")

    raw_u = os.getenv("OPTIONS_ALLOWED_UNDERLYINGS")
    if raw_u is not None and raw_u.strip():
        ot["allowed_underlyings"] = _parse_underlyings(raw_u)

    for env_key, yaml_key in (
        ("OPTIONS_MAX_PREMIUM_PER_TRADE", "max_premium_per_trade"),
        ("OPTIONS_MAX_CONTRACTS_PER_TRADE", "max_contracts_per_trade"),
        ("OPTIONS_MAX_TOTAL_PREMIUM_EXPOSURE", "max_total_premium_exposure"),
    ):
        if env_key in os.environ:
            v = os.getenv(env_key, "").strip()
            if v:
                ot[yaml_key] = v

    if "OPTIONS_PAPER_ONLY" in os.environ:
        ot["paper_only"] = os.getenv("OPTIONS_PAPER_ONLY", "").strip().lower() in (
            "1",
            "true",
            "yes",
            "on",
        )

    if "OPTIONS_ALLOW_SHORT" in os.environ:
        ot["allow_short"] = os.getenv("OPTIONS_ALLOW_SHORT", "").strip().lower() in (
            "1",
            "true",
            "yes",
            "on",
        )

    if "OPTIONS_ALLOW_SELL_TO_CLOSE" in os.environ:
        ot["allow_sell_to_close"] = os.getenv("OPTIONS_ALLOW_SELL_TO_CLOSE", "").strip().lower() in (
            "1",
            "true",
            "yes",
            "on",
        )


def options_trading_config(cfg: dict[str, Any]) -> dict[str, Any]:
    """Resolved options policy dict with safe defaults."""
    raw = cfg.get("options_trading")
    base: dict[str, Any] = dict(raw) if isinstance(raw, dict) else {}

    def dec(key: str, default: str) -> Decimal:
        try:
            return Decimal(str(base.get(key, default)))
        except Exception:  # noqa: BLE001
            return Decimal(default)

    try:
        max_contracts = int(Decimal(str(base.get("max_contracts_per_trade", "1"))))
    except Exception:  # noqa: BLE001
        max_contracts = 1
    max_contracts = max(1, max_contracts)

    allow_under = base.get("allowed_underlyings") or ["SPY"]
    if isinstance(allow_under, str):
        allow_under = _parse_underlyings(allow_under)
    allow_list = [str(x).strip().upper() for x in allow_under if str(x).strip()]

    return {
        "enabled": bool(base.get("enabled", False)),
        "paper_only": bool(base.get("paper_only", True)),
        "allowed_underlyings": allow_list,
        "max_premium_per_trade": dec("max_premium_per_trade", "200"),
        "max_contracts_per_trade": max_contracts,
        "max_total_premium_exposure": dec("max_total_premium_exposure", "500"),
        "allow_short": bool(base.get("allow_short", False)),
        "allow_sell_to_close": bool(base.get("allow_sell_to_close", True)),
    }
