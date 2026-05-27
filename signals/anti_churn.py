"""
signals/anti_churn.py
======================
Anti-churn signal gate (D115).

Stops three concrete classes of bleed observed in production paper trading
on 2026-05-19 (224 fills in 8h, $6.0M turnover on $1.18M NAV, $300 fees):

1. Duplicate-signal storm.
   The same (strategy, symbol, side, rounded confidence, rounded price)
   tuple emitted 11-20+ times in a single day produced 11-20 round-trip
   fills at near-identical prices, eating fees and bid/ask spread without
   any new information. See `volatility_regime buy AAPL conf=0.708` x36.

2. Cross-strategy contradiction.
   `volatility_regime` long AAPL/IWM/QQQ while `volume_flow` short the
   same names. Inventory ping-pongs and only the broker wins. The
   lower-confidence side is rejected; both sides are tombstoned for a
   cooldown so neither can re-enter immediately.

3. Post-fill churn.
   Same (broker, symbol) re-traded within seconds of a fill. Real
   strategies need conviction time; rapid re-entry is noise. A
   per-(broker, symbol) cooldown blocks re-entry until enough time has
   elapsed that the underlying feature snapshot can plausibly have moved.

All three gates are advisory at the signal layer — the risk engine
retains unconditional veto power downstream. They never let MORE through;
they only ever reject. They never apply to operator/allocator-driven
closes, reduce-only trims, or the global-edge maintenance path. State is
in-process only; restart resets the gate.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional


@dataclass(frozen=True)
class AntiChurnDecision:
    """Result of an anti-churn gate check."""

    allow: bool
    reason: str = ""
    details: dict = field(default_factory=dict)


@dataclass
class _RecentSignal:
    last_seen: datetime
    confidence: float


@dataclass
class _DirectionalState:
    strategy: str
    confidence: float
    timestamp: datetime


@dataclass
class _FillState:
    timestamp: datetime
    side: str


def _canonicalize_side(side: object) -> Optional[str]:
    s = str(side or "").strip().lower()
    if s in ("long", "buy"):
        return "buy"
    if s in ("short", "sell"):
        return "sell"
    return None


class AntiChurnGate:
    """
    Stateful gate for signal de-duplication, cross-strategy contradiction,
    and post-fill cooldown.

    Pure in-memory state. All mutations happen on the trading-loop thread
    (no asyncio await between read and write), so no locking required.
    """

    def __init__(self, config: Optional[dict] = None) -> None:
        cfg = config or {}

        self.dedup_enabled = bool(cfg.get("dedup_enabled", True))
        self.dedup_window_sec = float(cfg.get("dedup_window_sec", 90))
        self.dedup_confidence_dp = int(cfg.get("dedup_confidence_dp", 2))
        self.dedup_price_dp = int(cfg.get("dedup_price_dp", 4))

        self.contradiction_enabled = bool(cfg.get("contradiction_enabled", True))
        self.contradiction_window_sec = float(cfg.get("contradiction_window_sec", 300))

        self.post_fill_enabled = bool(cfg.get("post_fill_enabled", True))
        mode_cooldown_cfg = cfg.get("post_fill_cooldown_sec", {})
        if isinstance(mode_cooldown_cfg, (int, float, str)):
            uniform = float(mode_cooldown_cfg)
            self.post_fill_cooldown_by_mode: dict[str, float] = {
                "hunter": uniform,
                "trader": uniform,
                "defender": uniform,
            }
        elif isinstance(mode_cooldown_cfg, dict):
            self.post_fill_cooldown_by_mode = {
                "hunter": float(mode_cooldown_cfg.get("hunter", 120)),
                "trader": float(mode_cooldown_cfg.get("trader", 180)),
                "defender": float(mode_cooldown_cfg.get("defender", 600)),
            }
        else:
            self.post_fill_cooldown_by_mode = {
                "hunter": 120.0,
                "trader": 180.0,
                "defender": 600.0,
            }

        self.max_entries = int(cfg.get("max_entries", 50_000))

        self._recent: dict[tuple[str, str, str, str, str], _RecentSignal] = {}
        self._directional: dict[str, dict[str, _DirectionalState]] = {}
        self._last_fill: dict[tuple[str, str], _FillState] = {}
        self._contradiction_tombstone: dict[str, datetime] = {}

        self._stats: dict[str, int] = {
            "dedup_blocked": 0,
            "contradiction_blocked": 0,
            "post_fill_blocked": 0,
            "tombstone_blocked": 0,
            "allowed": 0,
            "passthrough_hold": 0,
        }

    # ---------------- check ----------------
    def check(
        self,
        *,
        strategy: str,
        symbol: str,
        side: object,
        confidence: float,
        suggested_price: Optional[float],
        broker: str,
        profile_mode: str = "hunter",
        now: Optional[datetime] = None,
        market_state_score: Optional[float] = None,
        recent_fill_rate_per_min: Optional[float] = None,
    ) -> AntiChurnDecision:
        """Decide whether a fresh signal candidate should proceed."""
        n = now or datetime.now(timezone.utc)
        sym = (symbol or "").upper()
        canonical = _canonicalize_side(side)
        if canonical is None:
            self._stats["passthrough_hold"] += 1
            return AntiChurnDecision(allow=True, reason="hold_passthrough")

        if self.contradiction_enabled:
            tomb = self._contradiction_tombstone.get(sym)
            if tomb is not None and tomb > n:
                self._stats["tombstone_blocked"] += 1
                return AntiChurnDecision(
                    allow=False,
                    reason="contradiction_tombstone",
                    details={"symbol": sym, "until": tomb.isoformat()},
                )

        if self.post_fill_enabled:
            # D141 — cooldown computed live from regime + recent fill rate.
            # Strong regime → shorter (catch the trend); high fill rate
            # on this symbol → longer (dampen churn). Falls back to the
            # static per-mode value when the dynamic_thresholds YAML
            # block is disabled.
            static_cooldown = self.post_fill_cooldown_by_mode.get(
                (profile_mode or "hunter").lower(),
                self.post_fill_cooldown_by_mode["hunter"],
            )
            try:
                from system.dynamic_thresholds import anti_churn_cooldown_sec
                cooldown = float(anti_churn_cooldown_sec(
                    mode=profile_mode or "hunter",
                    market_state_score=market_state_score or 0,
                    recent_fill_rate_per_min=recent_fill_rate_per_min or 0,
                    static_cooldown=static_cooldown,
                ))
            except Exception:  # noqa: BLE001 — never break the gate on a YAML hiccup
                cooldown = static_cooldown
            fill = self._last_fill.get((broker or "", sym))
            if fill is not None:
                elapsed = (n - fill.timestamp).total_seconds()
                if elapsed < cooldown:
                    self._stats["post_fill_blocked"] += 1
                    return AntiChurnDecision(
                        allow=False,
                        reason="post_fill_cooldown",
                        details={
                            "broker": broker,
                            "symbol": sym,
                            "elapsed_sec": elapsed,
                            "cooldown_sec": cooldown,
                            "last_fill_side": fill.side,
                        },
                    )

        if self.dedup_enabled:
            conf_key = self._round_conf(confidence)
            price_key = self._round_price(suggested_price)
            key = (str(strategy), sym, canonical, conf_key, price_key)
            recent = self._recent.get(key)
            if recent is not None:
                elapsed = (n - recent.last_seen).total_seconds()
                if elapsed < self.dedup_window_sec:
                    self._stats["dedup_blocked"] += 1
                    return AntiChurnDecision(
                        allow=False,
                        reason="identical_signal_dedup",
                        details={
                            "strategy": strategy,
                            "symbol": sym,
                            "side": canonical,
                            "confidence": conf_key,
                            "price": price_key,
                            "elapsed_sec": elapsed,
                            "window_sec": self.dedup_window_sec,
                        },
                    )

        if self.contradiction_enabled:
            sym_state = self._directional.get(sym) or {}
            opp = "sell" if canonical == "buy" else "buy"
            other = sym_state.get(opp)
            if other is not None:
                elapsed = (n - other.timestamp).total_seconds()
                if elapsed < self.contradiction_window_sec:
                    until = n + timedelta(seconds=self.contradiction_window_sec)
                    self._contradiction_tombstone[sym] = until
                    self._stats["contradiction_blocked"] += 1
                    if other.confidence >= float(confidence or 0.0):
                        return AntiChurnDecision(
                            allow=False,
                            reason="cross_strategy_contradiction",
                            details={
                                "symbol": sym,
                                "current_side": canonical,
                                "current_strategy": strategy,
                                "current_confidence": float(confidence or 0.0),
                                "blocking_strategy": other.strategy,
                                "blocking_side": opp,
                                "blocking_confidence": other.confidence,
                                "until": until.isoformat(),
                            },
                        )
                    return AntiChurnDecision(
                        allow=False,
                        reason="cross_strategy_contradiction",
                        details={
                            "symbol": sym,
                            "current_side": canonical,
                            "current_strategy": strategy,
                            "current_confidence": float(confidence or 0.0),
                            "displaced_strategy": other.strategy,
                            "displaced_side": opp,
                            "displaced_confidence": other.confidence,
                            "until": until.isoformat(),
                            "note": "current_won_but_tombstoned_both",
                        },
                    )

        self._stats["allowed"] += 1
        return AntiChurnDecision(allow=True, reason="passed")

    # ---------------- record signal ----------------
    def record_signal(
        self,
        *,
        strategy: str,
        symbol: str,
        side: object,
        confidence: float,
        suggested_price: Optional[float],
        now: Optional[datetime] = None,
    ) -> None:
        if not (self.dedup_enabled or self.contradiction_enabled):
            return
        n = now or datetime.now(timezone.utc)
        sym = (symbol or "").upper()
        canonical = _canonicalize_side(side)
        if canonical is None:
            return
        if self.dedup_enabled:
            conf_key = self._round_conf(confidence)
            price_key = self._round_price(suggested_price)
            self._recent[(str(strategy), sym, canonical, conf_key, price_key)] = _RecentSignal(
                last_seen=n, confidence=float(confidence or 0.0)
            )
        if self.contradiction_enabled:
            self._directional.setdefault(sym, {})
            self._directional[sym][canonical] = _DirectionalState(
                strategy=str(strategy), confidence=float(confidence or 0.0), timestamp=n
            )
        self._maybe_compact(n)

    # ---------------- record fill ----------------
    def record_fill(
        self,
        *,
        broker: str,
        symbol: str,
        side: object,
        is_reduce_only: bool = False,  # noqa: ARG002 - reserved for future side-aware policy
        now: Optional[datetime] = None,
    ) -> None:
        if not self.post_fill_enabled:
            return
        n = now or datetime.now(timezone.utc)
        sym = (symbol or "").upper()
        canonical = _canonicalize_side(side) or "buy"
        self._last_fill[(broker or "", sym)] = _FillState(timestamp=n, side=canonical)
        self._maybe_compact(n)

    # ---------------- helpers ----------------
    def _round_conf(self, value: object) -> str:
        try:
            v = float(value or 0.0)
        except (TypeError, ValueError):
            v = 0.0
        return f"{round(v, self.dedup_confidence_dp):.{self.dedup_confidence_dp}f}"

    def _round_price(self, value: Optional[float]) -> str:
        if value is None:
            return "_"
        try:
            v = float(value)
        except (TypeError, ValueError):
            return "_"
        return f"{round(v, self.dedup_price_dp):.{self.dedup_price_dp}f}"

    def _maybe_compact(self, now: datetime) -> None:
        if (
            len(self._recent) <= self.max_entries
            and len(self._last_fill) <= self.max_entries
            and len(self._directional) <= self.max_entries
        ):
            return
        horizon_sec = max(
            self.dedup_window_sec,
            self.contradiction_window_sec,
            max(self.post_fill_cooldown_by_mode.values(), default=600.0),
        ) * 4.0
        horizon = now - timedelta(seconds=horizon_sec)
        self._recent = {k: v for k, v in self._recent.items() if v.last_seen >= horizon}
        self._last_fill = {k: v for k, v in self._last_fill.items() if v.timestamp >= horizon}
        for sym in list(self._directional.keys()):
            self._directional[sym] = {
                sd: st for sd, st in self._directional[sym].items() if st.timestamp >= horizon
            }
            if not self._directional[sym]:
                del self._directional[sym]
        self._contradiction_tombstone = {
            sym: ts for sym, ts in self._contradiction_tombstone.items() if ts >= now
        }

    def snapshot(self) -> dict:
        return {
            "recent_signal_keys": len(self._recent),
            "last_fill_keys": len(self._last_fill),
            "directional_symbols": len(self._directional),
            "contradiction_tombstones_active": sum(
                1 for ts in self._contradiction_tombstone.values()
                if ts > datetime.now(timezone.utc)
            ),
            "dedup_enabled": self.dedup_enabled,
            "contradiction_enabled": self.contradiction_enabled,
            "post_fill_enabled": self.post_fill_enabled,
            "stats": dict(self._stats),
        }
