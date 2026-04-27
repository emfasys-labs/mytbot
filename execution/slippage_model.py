"""
execution/slippage_model.py
=============================
Wave 9 — slippage model with online updates.

Tracks an EWMA of |slippage_bps| per (broker, symbol) pair, plus a
fallback prior per (broker, asset_class) and a global prior. The router
queries this to add a learned slippage component on top of the impact
model in ``execution/impact.py``.

Slippage convention (consistent with `execution/router.py` Wave 9
telemetry): the absolute value of (avg_fill_price - reference_price)
expressed in bps, sign-agnostic — we care about the cost of execution,
not its direction.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Optional


@dataclass
class SlippageEstimate:
    bps: float
    samples: int
    source: str  # "broker_symbol" | "broker_asset" | "global" | "default"


@dataclass
class SlippageModel:
    """
    Three-tier prior:

      1. (broker, symbol)    — most specific, takes priority once N>=min_samples_specific.
      2. (broker, asset_class) — group-level fallback.
      3. global              — coarse fallback (all brokers).

    Each tier is an EWMA of |slippage_bps|. The decay is set per-tier:
    specific tiers update faster (more weight on recent observations)
    so the model adapts when an exchange's microstructure changes.
    """

    decay_specific: float = 0.10
    decay_group: float = 0.04
    decay_global: float = 0.01
    default_bps: float = 5.0
    min_samples_specific: int = 10

    _by_broker_symbol: dict[tuple[str, str], tuple[float, int]] = field(default_factory=dict)
    _by_broker_asset: dict[tuple[str, str], tuple[float, int]] = field(default_factory=dict)
    _global: tuple[float, int] = (5.0, 0)

    # ── prediction ──────────────────────────────────────────────────────────

    def estimate(
        self,
        *,
        broker: str,
        symbol: Optional[str] = None,
        asset_class: Optional[str] = None,
    ) -> SlippageEstimate:
        b = (broker or "").strip().lower()
        s = (symbol or "").strip().upper()
        ac = (asset_class or "").strip().lower()

        if s:
            key = (b, s)
            if key in self._by_broker_symbol:
                bps, n = self._by_broker_symbol[key]
                if n >= self.min_samples_specific:
                    return SlippageEstimate(bps=bps, samples=n, source="broker_symbol")

        if ac:
            key = (b, ac)
            if key in self._by_broker_asset:
                bps, n = self._by_broker_asset[key]
                if n > 0:
                    return SlippageEstimate(bps=bps, samples=n, source="broker_asset")

        gbps, gn = self._global
        if gn > 0:
            return SlippageEstimate(bps=gbps, samples=gn, source="global")
        return SlippageEstimate(bps=self.default_bps, samples=0, source="default")

    # ── online update ───────────────────────────────────────────────────────

    def update(
        self,
        *,
        broker: str,
        symbol: Optional[str],
        asset_class: Optional[str],
        observed_bps: float,
    ) -> None:
        """EWMA-update each tier with the new observation."""
        if observed_bps is None:
            return
        try:
            x = abs(float(observed_bps))
        except (TypeError, ValueError):
            return
        b = (broker or "").strip().lower()
        s = (symbol or "").strip().upper()
        ac = (asset_class or "").strip().lower()

        # Specific tier.
        if s:
            key = (b, s)
            prev_bps, prev_n = self._by_broker_symbol.get(key, (x, 0))
            new_bps = (1.0 - self.decay_specific) * prev_bps + self.decay_specific * x if prev_n > 0 else x
            self._by_broker_symbol[key] = (new_bps, prev_n + 1)

        # Group tier.
        if ac:
            key = (b, ac)
            prev_bps, prev_n = self._by_broker_asset.get(key, (x, 0))
            new_bps = (1.0 - self.decay_group) * prev_bps + self.decay_group * x if prev_n > 0 else x
            self._by_broker_asset[key] = (new_bps, prev_n + 1)

        # Global tier.
        gbps, gn = self._global
        new_bps = (1.0 - self.decay_global) * gbps + self.decay_global * x if gn > 0 else x
        self._global = (new_bps, gn + 1)

    # ── snapshot / restore (for control_state persistence) ──────────────────

    def snapshot(self) -> dict[str, object]:
        return {
            "by_broker_symbol": {f"{b}|{s}": [v[0], v[1]] for (b, s), v in self._by_broker_symbol.items()},
            "by_broker_asset": {f"{b}|{a}": [v[0], v[1]] for (b, a), v in self._by_broker_asset.items()},
            "global": [self._global[0], self._global[1]],
            "default_bps": self.default_bps,
        }

    def restore(self, snap: Mapping[str, object]) -> None:
        bs = snap.get("by_broker_symbol") or {}
        if isinstance(bs, dict):
            self._by_broker_symbol = {
                tuple(k.split("|", 1)): (float(v[0]), int(v[1]))  # type: ignore[arg-type, misc]
                for k, v in bs.items()
                if isinstance(k, str) and "|" in k and isinstance(v, (list, tuple)) and len(v) == 2
            }
        ba = snap.get("by_broker_asset") or {}
        if isinstance(ba, dict):
            self._by_broker_asset = {
                tuple(k.split("|", 1)): (float(v[0]), int(v[1]))  # type: ignore[arg-type, misc]
                for k, v in ba.items()
                if isinstance(k, str) and "|" in k and isinstance(v, (list, tuple)) and len(v) == 2
            }
        g = snap.get("global")
        if isinstance(g, (list, tuple)) and len(g) == 2:
            self._global = (float(g[0]), int(g[1]))
        if "default_bps" in snap:
            try:
                self.default_bps = float(snap["default_bps"])  # type: ignore[arg-type]
            except (TypeError, ValueError):
                pass
