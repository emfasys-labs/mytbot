"""
execution/venue_quality.py
============================
Wave 9 — broker / venue cost priors.

Layered defaults that the router consults when picking a venue:

- Per-broker fee bps (taker / maker).
- Per-broker default spread bps by asset class.
- Live slippage estimate (delegated to ``execution/slippage_model.py``).

The priors are configured in ``config/execution_models.yaml`` and can
be overridden at runtime as the router observes live fills.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Optional

from execution.slippage_model import SlippageEstimate, SlippageModel


@dataclass(frozen=True)
class FeePrior:
    taker_bps: float = 5.0
    maker_bps: float = 0.0


@dataclass(frozen=True)
class SpreadPrior:
    bps_by_asset_class: tuple[tuple[str, float], ...] = (
        ("equity", 1.0),
        ("etf", 1.0),
        ("bond", 4.0),
        ("forex", 0.5),
        ("crypto", 5.0),
        ("future", 1.0),
        ("option", 20.0),
        ("other", 5.0),
    )

    def for_asset(self, asset_class: str) -> float:
        ac = (asset_class or "").strip().lower()
        for k, v in self.bps_by_asset_class:
            if k == ac:
                return float(v)
        for k, v in self.bps_by_asset_class:
            if k == "other":
                return float(v)
        return 5.0


@dataclass
class VenuePriors:
    fees: dict[str, FeePrior] = field(default_factory=dict)
    spreads: dict[str, SpreadPrior] = field(default_factory=dict)
    slippage: SlippageModel = field(default_factory=SlippageModel)

    @classmethod
    def from_dict(cls, raw: Optional[Mapping[str, object]]) -> "VenuePriors":
        if not raw:
            return cls()
        sect = raw.get("venue_priors") if "venue_priors" in raw else raw  # type: ignore[union-attr]
        sect = dict(sect or {})

        fees: dict[str, FeePrior] = {}
        for broker, vals in (sect.get("fees") or {}).items():
            if not isinstance(vals, Mapping):
                continue
            fees[str(broker).lower()] = FeePrior(
                taker_bps=float(vals.get("taker_bps", 5.0)),
                maker_bps=float(vals.get("maker_bps", 0.0)),
            )

        spreads: dict[str, SpreadPrior] = {}
        for broker, vals in (sect.get("spreads") or {}).items():
            if not isinstance(vals, Mapping):
                continue
            entries = tuple((str(k).lower(), float(v)) for k, v in vals.items())
            spreads[str(broker).lower()] = SpreadPrior(bps_by_asset_class=entries)

        return cls(fees=fees, spreads=spreads, slippage=SlippageModel())

    # ── lookups ─────────────────────────────────────────────────────────────

    def fee_for(self, broker: str, *, taker: bool = True) -> float:
        f = self.fees.get((broker or "").strip().lower())
        if f is None:
            return 5.0
        return float(f.taker_bps if taker else f.maker_bps)

    def spread_for(self, broker: str, asset_class: str) -> float:
        sp = self.spreads.get((broker or "").strip().lower())
        if sp is None:
            return SpreadPrior().for_asset(asset_class)
        return sp.for_asset(asset_class)

    def slippage_for(
        self,
        *,
        broker: str,
        symbol: Optional[str] = None,
        asset_class: Optional[str] = None,
    ) -> SlippageEstimate:
        return self.slippage.estimate(broker=broker, symbol=symbol, asset_class=asset_class)
