"""
strategies/stat_arb_pairs.py
==============================
Wave 5 — research-grade statistical-arbitrage pairs strategy.

Differs from the existing ``strategies/pairs_trading.py`` heuristic in
three meaningful ways:

  1. Hedge ratio is *time-varying* via Kalman filter (or static via OLS).
  2. Entry / exit thresholds are cost-aware: round-trip transaction
     cost in bps drives the minimum z-score.
  3. Output is a ``LinkedOpportunity`` carrying both legs and a
     ``linkage_policy`` so the execution layer knows how to handle a
     partial fill on one leg (cancel sibling, hedge with the underlying,
     or flatten both).

Boundary discipline:

- ``enabled`` defaults to ``False`` and the YAML ships disabled.
- The strategy emits ``LinkedOpportunity`` only — orders are still
  produced via the standard signal → risk → execution pipeline by the
  allocator, never by this module.
- Spread state is NOT persisted here; ``portfolio/d015_replacement_context.py``
  or a future ``pair_state`` table is the right place. The strategy
  reads its own Kalman state from the operator's persistence layer.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Mapping, Optional

import numpy as np
import pandas as pd
import yaml

from core.models_runtime import (
    AssetClass,
    Opportunity,
    OpportunityComponents,
    Side,
    SignalCandidate,
    clip_decimal,
)
from models.pairs.johansen import engle_granger_test
from models.pairs.kalman import KalmanHedgeRatio
from models.pairs.risk import (
    SpreadBreakResult,
    detect_correlation_decay,
    detect_spread_break,
    transaction_cost_aware_thresholds,
)
from models.pairs.spread import compute_spread, half_life_ou, spread_zscore

logger = logging.getLogger(__name__)


DEFAULT_CONFIG_PATH = Path("config/pairs_trading.yaml")


# ── linked-leg policy ───────────────────────────────────────────────────────


class LinkagePolicy(Enum):
    """How execution should handle a partial fill on one leg."""

    CANCEL_SIBLING = "cancel_sibling"   # safest default — cancel the other leg
    HEDGE_WITH_INDEX = "hedge_with_index"  # advanced: hedge filled leg with the asset's benchmark
    FLATTEN_BOTH = "flatten_both"       # close any partial; abort the trade


@dataclass
class LinkedOpportunity:
    """
    A pair trade as two ``Opportunity`` legs plus linkage metadata.

    The downstream allocator + execution layer (Wave-5 wiring follow-up)
    are expected to honour ``linkage_policy`` when one leg fails.
    """

    leg_long: Opportunity
    leg_short: Opportunity
    pair_id: str
    linkage_policy: LinkagePolicy = LinkagePolicy.CANCEL_SIBLING
    spread_zscore: float = 0.0
    half_life_bars: Optional[float] = None
    hedge_ratio: float = 1.0
    entry_z_threshold: float = 2.0
    exit_z_threshold: float = 0.0
    metadata: dict[str, object] = field(default_factory=dict)


# ── config ──────────────────────────────────────────────────────────────────


@dataclass
class StatArbPairsConfig:
    enabled: bool = False
    use_kalman: bool = True
    z_window: int = 60
    correlation_window: int = 60
    correlation_floor: float = 0.5
    z_threshold_break: float = 4.0
    half_life_ceiling_bars: float = 200.0
    round_trip_cost_bps: float = 10.0
    min_entry_z: float = 1.5
    safety_multiplier: float = 1.2
    linkage_policy: LinkagePolicy = LinkagePolicy.CANCEL_SIBLING
    strategy_name: str = "stat_arb_pairs"
    preferred_broker: str = "ibkr"
    confidence_floor: float = 0.10
    confidence_ceiling: float = 0.85

    @classmethod
    def from_dict(cls, raw: Optional[Mapping[str, object]]) -> "StatArbPairsConfig":
        if not raw:
            return cls()
        sect = raw.get("stat_arb_pairs") if "stat_arb_pairs" in raw else raw  # type: ignore[union-attr]
        sect = dict(sect or {})
        lp_raw = str(sect.get("linkage_policy", "cancel_sibling")).strip().lower()
        try:
            lp = LinkagePolicy(lp_raw)
        except ValueError:
            lp = LinkagePolicy.CANCEL_SIBLING
        return cls(
            enabled=bool(sect.get("enabled", False)),
            use_kalman=bool(sect.get("use_kalman", True)),
            z_window=int(sect.get("z_window", 60)),
            correlation_window=int(sect.get("correlation_window", 60)),
            correlation_floor=float(sect.get("correlation_floor", 0.5)),
            z_threshold_break=float(sect.get("z_threshold_break", 4.0)),
            half_life_ceiling_bars=float(sect.get("half_life_ceiling_bars", 200.0)),
            round_trip_cost_bps=float(sect.get("round_trip_cost_bps", 10.0)),
            min_entry_z=float(sect.get("min_entry_z", 1.5)),
            safety_multiplier=float(sect.get("safety_multiplier", 1.2)),
            linkage_policy=lp,
            strategy_name=str(sect.get("strategy_name", "stat_arb_pairs")),
            preferred_broker=str(sect.get("preferred_broker", "ibkr")),
            confidence_floor=float(sect.get("confidence_floor", 0.10)),
            confidence_ceiling=float(sect.get("confidence_ceiling", 0.85)),
        )

    @classmethod
    def load(cls, path: Path | str = DEFAULT_CONFIG_PATH) -> "StatArbPairsConfig":
        p = Path(path)
        if not p.exists():
            return cls()
        try:
            raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise RuntimeError(f"could not parse {p}: {exc}") from exc
        return cls.from_dict(raw)


# ── strategy ────────────────────────────────────────────────────────────────


class StatArbPairsStrategy:
    """Compute a single pair's verdict and emit a ``LinkedOpportunity`` (or None)."""

    def __init__(self, config: Optional[StatArbPairsConfig] = None) -> None:
        self.config = config or StatArbPairsConfig()

    @property
    def enabled(self) -> bool:
        return self.config.enabled

    def evaluate(
        self,
        *,
        leg_a_symbol: str,
        leg_b_symbol: str,
        leg_a_prices: pd.Series,
        leg_b_prices: pd.Series,
        asset_class: str = "equity",
        as_of: Optional[datetime] = None,
        pair_id: Optional[str] = None,
    ) -> Optional[LinkedOpportunity]:
        """
        Return a ``LinkedOpportunity`` when:
          - the pair is cointegrated (Engle-Granger screen),
          - correlation hasn't decayed below the floor,
          - the spread isn't broken,
          - the latest |z-score| exceeds the cost-aware entry threshold.

        Otherwise return ``None``.
        """
        if not self.config.enabled:
            return None

        ts = as_of or datetime.now(timezone.utc)

        # --- align inputs --------------------------------------------------
        df = pd.concat(
            [leg_a_prices.astype(float), leg_b_prices.astype(float)],
            axis=1,
            join="inner",
        ).dropna()
        if len(df) < max(60, self.config.z_window):
            return None
        df.columns = ["a", "b"]

        # --- correlation gate ---------------------------------------------
        decayed, latest_corr = detect_correlation_decay(
            df["a"], df["b"], window=self.config.correlation_window, floor=self.config.correlation_floor
        )
        if decayed:
            logger.debug("stat_arb_pairs | %s/%s correlation decayed (%.3f)", leg_a_symbol, leg_b_symbol, latest_corr or 0.0)
            return None

        # --- cointegration screen + hedge ratio ---------------------------
        eg = engle_granger_test(df["a"], df["b"])
        if not eg.is_cointegrated_5pct:
            return None
        beta_static = float(eg.beta)
        intercept_static = float(eg.intercept)

        if self.config.use_kalman:
            kf = KalmanHedgeRatio()
            params = kf.run(df["a"], df["b"])
            beta_series = params["beta"]
            intercept_series = params["intercept"]
            hedge_ratio = float(beta_series.iloc[-1])
        else:
            beta_series = pd.Series(beta_static, index=df.index)
            intercept_series = pd.Series(intercept_static, index=df.index)
            hedge_ratio = beta_static

        spread = compute_spread(df["a"], df["b"], beta=beta_series, intercept=intercept_series)
        z = spread_zscore(spread, window=self.config.z_window)
        latest_z = float(z.iloc[-1]) if not z.empty and not pd.isna(z.iloc[-1]) else 0.0

        # --- spread-break detector ---------------------------------------
        break_check = detect_spread_break(
            z,
            spread,
            z_threshold=self.config.z_threshold_break,
            half_life_ceiling_bars=self.config.half_life_ceiling_bars,
            lookback=self.config.z_window,
        )
        if break_check.is_broken:
            logger.info(
                "stat_arb_pairs | %s/%s spread broken | z=%.2f hl=%s",
                leg_a_symbol, leg_b_symbol, latest_z, break_check.half_life_bars,
            )
            return None

        # --- cost-aware threshold ----------------------------------------
        sigma = float(spread.dropna().std(ddof=1))
        entry_z, exit_z = transaction_cost_aware_thresholds(
            spread_sigma=sigma,
            round_trip_cost_bps=self.config.round_trip_cost_bps,
            min_entry_z=self.config.min_entry_z,
            safety_multiplier=self.config.safety_multiplier,
        )

        # --- entry decision ----------------------------------------------
        if abs(latest_z) < entry_z:
            return None

        # When z is positive, the spread (a - β*b) is high → short A, long B.
        # When z is negative, long A, short B.
        long_symbol, short_symbol = (
            (leg_b_symbol, leg_a_symbol) if latest_z > 0 else (leg_a_symbol, leg_b_symbol)
        )

        confidence = self._z_to_confidence(abs(latest_z), entry_z)
        hl = half_life_ou(spread)

        pair_id = pair_id or f"{leg_a_symbol}|{leg_b_symbol}"
        meta_common: dict[str, object] = {
            "stat_arb_pair_id": pair_id,
            "stat_arb_zscore": latest_z,
            "stat_arb_hedge_ratio": hedge_ratio,
            "stat_arb_half_life_bars": hl,
            "stat_arb_correlation": latest_corr,
            "stat_arb_entry_z": entry_z,
            "stat_arb_exit_z": exit_z,
            "stat_arb_linkage_policy": self.config.linkage_policy.value,
            "stat_arb_strategy": self.config.strategy_name,
        }

        leg_long = self._make_leg(
            symbol=long_symbol,
            side="long",
            confidence=confidence,
            asset_class=asset_class,
            ts=ts,
            metadata={**meta_common, "stat_arb_role": "long_leg"},
        )
        leg_short = self._make_leg(
            symbol=short_symbol,
            side="short",
            confidence=confidence,
            asset_class=asset_class,
            ts=ts,
            metadata={**meta_common, "stat_arb_role": "short_leg"},
        )

        return LinkedOpportunity(
            leg_long=leg_long,
            leg_short=leg_short,
            pair_id=pair_id,
            linkage_policy=self.config.linkage_policy,
            spread_zscore=latest_z,
            half_life_bars=hl,
            hedge_ratio=hedge_ratio,
            entry_z_threshold=entry_z,
            exit_z_threshold=exit_z,
            metadata=meta_common,
        )

    # ── helpers ─────────────────────────────────────────────────────────────

    def _z_to_confidence(self, abs_z: float, entry_z: float) -> Decimal:
        """Logistic squash of (|z| - entry_z) into [floor, ceiling]."""
        import math

        x = max(0.0, abs_z - entry_z)
        p = 1.0 / (1.0 + math.exp(-x))  # 0.5 at entry, ~1.0 deep in tail
        mn, mx = self.config.confidence_floor, self.config.confidence_ceiling
        return Decimal(str(mn + (mx - mn) * p))

    def _make_leg(
        self,
        *,
        symbol: str,
        side: str,
        confidence: Decimal,
        asset_class: str,
        ts: datetime,
        metadata: dict,
    ) -> Opportunity:
        ac = (asset_class or "other").strip().lower()
        if ac not in ("equity", "etf", "bond", "forex", "crypto", "future", "option", "other"):
            ac = "other"
        side_typed: Side = "long" if side == "long" else "short"
        c = clip_decimal(confidence, Decimal("0"), Decimal("1"))
        return Opportunity(
            symbol=symbol,
            asset_class=ac,  # type: ignore[arg-type]
            side=side_typed,
            timestamp=ts,
            opportunity_score=c,
            urgency_score=clip_decimal(c * Decimal("0.8"), Decimal("0"), Decimal("1")),
            confidence=c,
            components=OpportunityComponents(),
            tags=[self.config.strategy_name],
            metadata={"strategy": self.config.strategy_name, **metadata},
        )
