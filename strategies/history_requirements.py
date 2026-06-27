"""Feature-history requirements derived from enabled strategy configuration."""

from __future__ import annotations

from typing import Any


def enabled_strategy_history_bars(strategies_cfg: dict[str, Any]) -> int:
    """Resolve the longest enabled strategy dependency, including current bar."""
    requirements = [1]
    for cfg in (strategies_cfg.get("strategies") or {}).values():
        if not isinstance(cfg, dict) or not bool(cfg.get("enabled", False)):
            continue
        if cfg.get("lookback_periods") is not None:
            requirements.append(int(cfg["lookback_periods"]) + 1)
        if cfg.get("lookback_bars") is not None:
            requirements.append(int(cfg["lookback_bars"]) + 1)
        if cfg.get("entry_lookback") is not None:
            requirements.append(int(cfg["entry_lookback"]) + 2)
        if cfg.get("slow_period") is not None:
            requirements.append(int(cfg["slow_period"]) + 1)
        if cfg.get("volume_lookback") is not None:
            requirements.append(int(cfg["volume_lookback"]) + 1)
    return max(requirements)
