"""Rolling strategy expectancy quarantine.

The regime multiplier answers "does this strategy fit the market?".
The quarantine multiplier answers "has this strategy recently earned the
right to open new risk?". They compose multiplicatively in sizing:

    final_strategy_weight = regime_mult * quarantine_mult

All thresholds come from ``config/strategies.yaml::strategy_quarantine``.
If the block is missing or incomplete, the function returns a neutral pass.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping


@dataclass(frozen=True)
class StrategyQuarantineDecision:
    state: str
    multiplier: Decimal
    reason: str
    fills: int
    net_pnl: Decimal
    pnl_per_fill: Decimal
    win_rate: Decimal


_NEUTRAL = StrategyQuarantineDecision(
    state="normal",
    multiplier=Decimal("1"),
    reason="strategy_quarantine_neutral",
    fills=0,
    net_pnl=Decimal("0"),
    pnl_per_fill=Decimal("0"),
    win_rate=Decimal("0"),
)


def _decimal(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _complete(cfg: Mapping[str, Any]) -> bool:
    required = (
        "enabled",
        "min_fills",
        "reduced_pnl_per_fill",
        "blocked_pnl_per_fill",
        "reduce_only_pnl_per_fill",
        "reduced_win_rate",
        "blocked_win_rate",
        "reduce_only_win_rate",
        "multipliers",
    )
    if any(k not in cfg for k in required):
        return False
    mult = cfg.get("multipliers")
    if not isinstance(mult, Mapping):
        return False
    return all(k in mult for k in ("normal", "reduced_size", "blocked_new_opens", "reduce_only"))


def decide_strategy_quarantine(
    strategy: str,
    stats: Mapping[str, Any] | None,
    cfg: Mapping[str, Any] | None,
) -> StrategyQuarantineDecision:
    if not isinstance(cfg, Mapping) or not bool(cfg.get("enabled", False)) or not _complete(cfg):
        return _NEUTRAL
    stats = stats or {}
    try:
        fills = int(stats.get("fills", 0) or 0)
    except (TypeError, ValueError):
        fills = 0
    min_fills = int(cfg.get("min_fills") or 0)
    net = _decimal(stats.get("net_pnl")) or Decimal("0")
    win_rate = _decimal(stats.get("win_rate")) or Decimal("0")
    pnl_per_fill = net / Decimal(fills) if fills > 0 else Decimal("0")
    if fills < min_fills:
        return StrategyQuarantineDecision(
            state="learning",
            multiplier=_decimal((cfg.get("multipliers") or {}).get("normal")) or Decimal("1"),
            reason="sample_below_min_fills",
            fills=fills,
            net_pnl=net,
            pnl_per_fill=pnl_per_fill,
            win_rate=win_rate,
        )

    thresholds = {
        "reduce_only": (
            _decimal(cfg.get("reduce_only_pnl_per_fill")),
            _decimal(cfg.get("reduce_only_win_rate")),
        ),
        "blocked_new_opens": (
            _decimal(cfg.get("blocked_pnl_per_fill")),
            _decimal(cfg.get("blocked_win_rate")),
        ),
        "reduced_size": (
            _decimal(cfg.get("reduced_pnl_per_fill")),
            _decimal(cfg.get("reduced_win_rate")),
        ),
    }
    mults = cfg.get("multipliers") or {}
    for state in ("reduce_only", "blocked_new_opens", "reduced_size"):
        pnl_thr, wr_thr = thresholds[state]
        if pnl_thr is None or wr_thr is None:
            return _NEUTRAL
        if pnl_per_fill <= pnl_thr or win_rate <= wr_thr:
            return StrategyQuarantineDecision(
                state=state,
                multiplier=_decimal(mults.get(state)) or Decimal("0"),
                reason=f"{strategy}: {state}",
                fills=fills,
                net_pnl=net,
                pnl_per_fill=pnl_per_fill,
                win_rate=win_rate,
            )

    return StrategyQuarantineDecision(
        state="normal",
        multiplier=_decimal(mults.get("normal")) or Decimal("1"),
        reason="strategy_expectancy_healthy",
        fills=fills,
        net_pnl=net,
        pnl_per_fill=pnl_per_fill,
        win_rate=win_rate,
    )
