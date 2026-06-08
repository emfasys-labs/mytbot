"""
backtest/edge_gate.py
=====================

Strategy edge gate (D157).

PRINCIPLE (production practice): a strategy does not get capital until it has
demonstrated POSITIVE EXPECTANCY AFTER COSTS in out-of-sample backtest. The
D156 diagnosis showed several enabled strategies were near-breakeven or
worse; nothing stopped them deploying capital and bleeding. This module is
the gate.

Two halves:

  1. EVALUATION (offline, expensive) — run the existing walk-forward backtest
     harness for each strategy across the symbol universe, aggregate the
     out-of-sample windows into per-strategy edge metrics, and decide a
     verdict (allowed / reduced / blocked / insufficient_data). Driven by
     ``scripts/run_edge_gate.py`` on a schedule; writes a small JSON registry.

  2. ENFORCEMENT (live, cheap) — the trading loop loads the registry and
     gates capital: a ``blocked`` strategy's candidates are dropped; a
     ``reduced`` strategy's conviction is down-weighted (it feeds the D156
     orchestrator's strategy-trust *prior*, combined with live P&L). The
     verdict is an *a-priori* trust; the orchestrator's recent-P&L trust is
     the *posterior* update.

Design notes:
  * Pure decision logic (``decide_verdict``) + a thin atomic-JSON registry,
    both fully unit-testable. All money is ``Decimal`` (rule 3).
  * Fail-soft: a strategy with no verdict, or with too few trades to judge,
    is treated by ``unproven_policy`` (default ``reduce`` — half size — so a
    cold start does not halt all trading; set ``block`` for the strict
    "prove it first" stance).
  * Never bypasses risk; only ever REDUCES or REMOVES capital, never adds.
  * The cost model is the backtest's ``fee_bps`` / ``slippage_bps`` — set
    these to realistic LIVE costs so the gate proves edge survives reality,
    not the optimistic paper costs.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable

VERDICT_ALLOWED = "allowed"
VERDICT_REDUCED = "reduced"
VERDICT_BLOCKED = "blocked"
VERDICT_INSUFFICIENT = "insufficient_data"


def _dec(x: Any, default: str = "0") -> Decimal:
    try:
        return Decimal(str(x))
    except Exception:  # noqa: BLE001
        return Decimal(default)


# ─────────────────────────────────────────────────────────────────────────────
# Config + metrics + verdict
# ─────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class EdgeGateThresholds:
    enabled: bool = False
    min_trades: int = 30
    # Below this fraction of profitable out-of-sample windows → blocked.
    block_consistency: Decimal = Decimal("0.45")
    # At/above these (AND positive expectancy) → allowed (full size).
    allow_consistency: Decimal = Decimal("0.55")
    allow_profit_factor: Decimal = Decimal("1.10")
    # Capital multiplier applied to a "reduced" strategy.
    reduced_multiplier: Decimal = Decimal("0.50")
    # What to do with a strategy that has too few trades to judge.
    unproven_policy: str = "reduce"  # "reduce" | "block"

    @classmethod
    def from_yaml(cls, raw: dict[str, Any] | None) -> "EdgeGateThresholds":
        raw = raw or {}
        kwargs: dict[str, Any] = {"enabled": bool(raw.get("enabled", False))}
        if raw.get("min_trades") is not None:
            kwargs["min_trades"] = int(raw["min_trades"])
        for key in (
            "block_consistency",
            "allow_consistency",
            "allow_profit_factor",
            "reduced_multiplier",
        ):
            if raw.get(key) is not None:
                kwargs[key] = _dec(raw[key])
        up = str(raw.get("unproven_policy", "reduce")).strip().lower()
        kwargs["unproven_policy"] = up if up in ("reduce", "block") else "reduce"
        return cls(**kwargs)


@dataclass
class StrategyEdgeMetrics:
    """Aggregated out-of-sample backtest stats for one strategy."""

    strategy: str
    symbols_evaluated: int = 0
    windows: int = 0
    profitable_windows: int = 0
    total_trades: int = 0
    total_net_pnl: Decimal = Decimal("0")
    gross_profit: Decimal = Decimal("0")   # sum of positive window pnl
    gross_loss: Decimal = Decimal("0")     # sum of |negative window pnl|
    avg_win_rate: float = 0.0

    @property
    def consistency(self) -> Decimal:
        if self.windows <= 0:
            return Decimal("0")
        return Decimal(self.profitable_windows) / Decimal(self.windows)

    @property
    def expectancy_per_trade(self) -> Decimal:
        if self.total_trades <= 0:
            return Decimal("0")
        return self.total_net_pnl / Decimal(self.total_trades)

    @property
    def profit_factor(self) -> Decimal:
        if self.gross_loss <= 0:
            # No losing windows: profitable if there is any gross profit.
            return Decimal("999") if self.gross_profit > 0 else Decimal("0")
        return self.gross_profit / self.gross_loss


@dataclass
class EdgeVerdict:
    strategy: str
    verdict: str
    size_multiplier: Decimal
    allow_new_capital: bool
    reason: str
    metrics: dict[str, Any] = field(default_factory=dict)
    computed_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["size_multiplier"] = str(self.size_multiplier)
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "EdgeVerdict":
        return cls(
            strategy=str(d.get("strategy", "")),
            verdict=str(d.get("verdict", VERDICT_INSUFFICIENT)),
            size_multiplier=_dec(d.get("size_multiplier", "0"), "0"),
            allow_new_capital=bool(d.get("allow_new_capital", False)),
            reason=str(d.get("reason", "")),
            metrics=dict(d.get("metrics", {}) or {}),
            computed_at=str(d.get("computed_at", "")),
        )


def decide_verdict(
    metrics: StrategyEdgeMetrics,
    thresholds: EdgeGateThresholds,
    *,
    now: datetime | None = None,
) -> EdgeVerdict:
    """Pure decision: aggregated metrics + thresholds → verdict. Never raises."""
    ts = (now or datetime.now(timezone.utc)).isoformat()
    m_blob = {
        "symbols_evaluated": metrics.symbols_evaluated,
        "windows": metrics.windows,
        "profitable_windows": metrics.profitable_windows,
        "total_trades": metrics.total_trades,
        "total_net_pnl": str(metrics.total_net_pnl),
        "expectancy_per_trade": str(metrics.expectancy_per_trade),
        "consistency": str(metrics.consistency),
        "profit_factor": str(metrics.profit_factor),
        "avg_win_rate": round(float(metrics.avg_win_rate), 4),
    }

    def _mk(verdict: str, mult: Decimal, allow: bool, reason: str) -> EdgeVerdict:
        return EdgeVerdict(
            strategy=metrics.strategy,
            verdict=verdict,
            size_multiplier=mult,
            allow_new_capital=allow,
            reason=reason,
            metrics=m_blob,
            computed_at=ts,
        )

    # Too few trades to judge → unproven policy.
    if metrics.total_trades < thresholds.min_trades:
        if thresholds.unproven_policy == "block":
            return _mk(VERDICT_INSUFFICIENT, Decimal("0"), False,
                       f"only {metrics.total_trades} trades (<{thresholds.min_trades}); unproven_policy=block")
        return _mk(VERDICT_INSUFFICIENT, thresholds.reduced_multiplier, True,
                   f"only {metrics.total_trades} trades (<{thresholds.min_trades}); unproven_policy=reduce")

    exp = metrics.expectancy_per_trade
    cons = metrics.consistency
    pf = metrics.profit_factor

    # Clear no-edge → block.
    if exp <= 0 or cons < thresholds.block_consistency:
        return _mk(VERDICT_BLOCKED, Decimal("0"), False,
                   f"expectancy/trade={exp} consistency={cons} (block<{thresholds.block_consistency})")

    # Proven edge → full size.
    if cons >= thresholds.allow_consistency and pf >= thresholds.allow_profit_factor and exp > 0:
        return _mk(VERDICT_ALLOWED, Decimal("1"), True,
                   f"expectancy/trade={exp} consistency={cons} profit_factor={pf}")

    # Positive but weak/inconsistent → reduced.
    return _mk(VERDICT_REDUCED, thresholds.reduced_multiplier, True,
               f"positive but weak: expectancy/trade={exp} consistency={cons} profit_factor={pf}")


def aggregate_walk_forward(
    strategy: str,
    window_results: Iterable[Any],
    *,
    symbols_evaluated: int,
) -> StrategyEdgeMetrics:
    """Fold a flat list of per-window BacktestResult into StrategyEdgeMetrics.

    ``window_results`` items need ``trades``, ``net_pnl``, ``win_rate``
    attributes (``backtest.harness.BacktestResult``). Windows with zero
    trades are ignored for consistency/profit-factor (they carry no signal).
    """
    m = StrategyEdgeMetrics(strategy=strategy, symbols_evaluated=symbols_evaluated)
    wr_sum = 0.0
    wr_n = 0
    for r in window_results:
        trades = int(getattr(r, "trades", 0) or 0)
        if trades <= 0:
            continue
        pnl = _dec(getattr(r, "net_pnl", 0))
        m.windows += 1
        m.total_trades += trades
        m.total_net_pnl += pnl
        if pnl >= 0:
            m.profitable_windows += 1
            m.gross_profit += pnl
        else:
            m.gross_loss += -pnl
        wr_sum += float(getattr(r, "win_rate", 0.0) or 0.0)
        wr_n += 1
    m.avg_win_rate = (wr_sum / wr_n) if wr_n else 0.0
    return m


# ─────────────────────────────────────────────────────────────────────────────
# Registry (atomic JSON persistence)
# ─────────────────────────────────────────────────────────────────────────────
DEFAULT_REGISTRY_PATH = "data/state/edge_gate_verdicts.json"


class EdgeGateRegistry:
    """Loads/saves per-strategy verdicts as one atomic JSON file.

    Enforcement reads are cheap (one file load, cached by mtime by the
    caller if desired). Writes are atomic (temp file + os.replace) so a
    concurrent reader never sees a half-written file.
    """

    def __init__(self, path: str | os.PathLike[str] = DEFAULT_REGISTRY_PATH):
        self.path = Path(path)
        self._verdicts: dict[str, EdgeVerdict] = {}

    # ---- load / save -----------------------------------------------------
    def load(self) -> "EdgeGateRegistry":
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError, OSError):
            self._verdicts = {}
            return self
        verdicts: dict[str, EdgeVerdict] = {}
        for name, vd in (raw.get("verdicts", {}) or {}).items():
            try:
                verdicts[str(name)] = EdgeVerdict.from_dict(vd)
            except Exception:  # noqa: BLE001
                continue
        self._verdicts = verdicts
        return self

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "verdicts": {name: v.to_dict() for name, v in self._verdicts.items()},
        }
        blob = json.dumps(payload, indent=2, sort_keys=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(blob)
            os.replace(tmp, self.path)
        finally:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass

    # ---- mutation --------------------------------------------------------
    def set_verdict(self, verdict: EdgeVerdict) -> None:
        self._verdicts[verdict.strategy] = verdict

    # ---- enforcement reads ----------------------------------------------
    def verdict_for(self, strategy: str) -> EdgeVerdict | None:
        return self._verdicts.get(strategy)

    def all_verdicts(self) -> dict[str, EdgeVerdict]:
        return dict(self._verdicts)

    def is_blocked(self, strategy: str, thresholds: EdgeGateThresholds) -> bool:
        """True iff a strategy must NOT receive fresh capital.

        Unknown strategy (no verdict yet) follows ``unproven_policy``.
        """
        v = self._verdicts.get(strategy)
        if v is None:
            return thresholds.unproven_policy == "block"
        return not v.allow_new_capital

    def size_multiplier_for(self, strategy: str, thresholds: EdgeGateThresholds) -> Decimal:
        """A-priori capital multiplier for a strategy (1.0 if proven, 0 if blocked)."""
        v = self._verdicts.get(strategy)
        if v is None:
            return Decimal("0") if thresholds.unproven_policy == "block" else thresholds.reduced_multiplier
        return v.size_multiplier
