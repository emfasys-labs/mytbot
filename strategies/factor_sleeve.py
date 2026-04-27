"""
strategies/factor_sleeve.py
============================
Wave 3 — cross-sectional factor sleeve.

Unlike the per-symbol momentum / mean-reversion strategies, the factor
sleeve operates on a *universe at once*: it ranks symbols
cross-sectionally and emits long signals for the top-N composite
scorers and (optionally) short signals for the bottom-N.

Boundary discipline:

- The sleeve produces ``SignalCandidate`` only — never orders. Every
  candidate still has to clear ``signals/engine.py`` →
  ``signals/opportunity_engine.py`` → risk → execution like any other.
- ``enabled`` defaults to ``False`` and the YAML config ships disabled.
- Asset-class neutralisation is on by default so equity factors don't
  drown out crypto factors when the universe is mixed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Mapping, Optional

import pandas as pd
import yaml

from core.models_runtime import AssetClass, SignalCandidate, Side, clip_decimal
from data.factor_features import build_price_factors
from data.fundamental_features import build_fundamental_factors
from signals.factor_scoring import (
    DEFAULT_BLEND,
    FactorBlend,
    FactorScores,
    blend_from_config,
    composite_factor_score,
)

logger = logging.getLogger(__name__)


DEFAULT_CONFIG_PATH = Path("config/factor_sleeve.yaml")


# ── config ──────────────────────────────────────────────────────────────────


@dataclass
class FactorSleeveConfig:
    enabled: bool = False
    long_top_n: int = 10
    short_bottom_n: int = 0  # 0 ⇒ long-only
    neutralise_by_asset_class: bool = True
    treat_missing: str = "zero"  # "zero" | "drop"
    confidence_floor: float = 0.05
    confidence_ceiling: float = 0.85
    blend: FactorBlend = field(default_factory=lambda: DEFAULT_BLEND)
    strategy_name: str = "factor_sleeve"
    preferred_broker: str = "ibkr"

    @classmethod
    def from_dict(cls, raw: Optional[Mapping[str, object]]) -> "FactorSleeveConfig":
        if not raw:
            return cls()
        sect = raw.get("factor_sleeve") if "factor_sleeve" in raw else raw  # type: ignore[union-attr]
        sect = dict(sect or {})
        return cls(
            enabled=bool(sect.get("enabled", False)),
            long_top_n=int(sect.get("long_top_n", 10)),
            short_bottom_n=int(sect.get("short_bottom_n", 0)),
            neutralise_by_asset_class=bool(sect.get("neutralise_by_asset_class", True)),
            treat_missing=str(sect.get("treat_missing", "zero")),
            confidence_floor=float(sect.get("confidence_floor", 0.05)),
            confidence_ceiling=float(sect.get("confidence_ceiling", 0.85)),
            blend=blend_from_config(sect.get("blend")),
            strategy_name=str(sect.get("strategy_name", "factor_sleeve")),
            preferred_broker=str(sect.get("preferred_broker", "ibkr")),
        )

    @classmethod
    def load(cls, path: Path | str = DEFAULT_CONFIG_PATH) -> "FactorSleeveConfig":
        p = Path(path)
        if not p.exists():
            return cls()
        try:
            raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise RuntimeError(f"could not parse {p}: {exc}") from exc
        return cls.from_dict(raw)


# ── sleeve ──────────────────────────────────────────────────────────────────


@dataclass
class FactorUniverseInput:
    """One symbol's row in the universe snapshot."""

    symbol: str
    asset_class: str
    close: pd.Series
    benchmark_close: Optional[pd.Series] = None
    fundamentals: Optional[Mapping[str, object]] = None


class FactorSleeve:
    """Long top-N / short bottom-N composite factor strategy."""

    def __init__(self, config: Optional[FactorSleeveConfig] = None):
        self.config = config or FactorSleeveConfig()

    @property
    def enabled(self) -> bool:
        return self.config.enabled

    def evaluate(
        self,
        universe: list[FactorUniverseInput],
        *,
        as_of: Optional[datetime] = None,
    ) -> tuple[list[SignalCandidate], FactorScores]:
        """
        Score every symbol in ``universe`` and return:
          - the list of ``SignalCandidate`` (long/short) for top/bottom N,
          - the full ``FactorScores`` (so the dashboard can render
            attribution even for symbols that did not generate a trade).

        Returns empty list + empty scores when the sleeve is disabled.
        """
        if not self.config.enabled:
            return [], FactorScores()

        ts = as_of or datetime.now(timezone.utc)

        per_symbol_factors: dict[str, dict[str, Optional[float]]] = {}
        groups: dict[str, str] = {}
        for row in universe:
            price_factors = build_price_factors(row.close, benchmark_close=row.benchmark_close)
            fund_factors = build_fundamental_factors(row.fundamentals)
            merged: dict[str, Optional[float]] = {}
            merged.update(price_factors)
            merged.update(fund_factors)
            per_symbol_factors[row.symbol] = merged
            groups[row.symbol] = (row.asset_class or "other").strip().lower()

        scores = composite_factor_score(
            per_symbol_factors=per_symbol_factors,
            blend=self.config.blend,
            groups=groups if self.config.neutralise_by_asset_class else None,
            treat_missing=self.config.treat_missing,
        )

        long_picks = scores.top_n(self.config.long_top_n) if self.config.long_top_n > 0 else []
        short_picks = (
            scores.bottom_n(self.config.short_bottom_n) if self.config.short_bottom_n > 0 else []
        )

        # Map composite z-score → confidence in [floor, ceiling] via a
        # logistic squash so extreme z's don't blow past the ceiling.
        def _conf(z: float) -> Decimal:
            import math

            p = 1.0 / (1.0 + math.exp(-float(z)))
            mn, mx = self.config.confidence_floor, self.config.confidence_ceiling
            return Decimal(str(mn + (mx - mn) * p))

        candidates: list[SignalCandidate] = []
        sym_to_row = {r.symbol: r for r in universe}

        for sym, z in long_picks:
            row = sym_to_row[sym]
            md = self._build_metadata(sym, scores, side="long", composite_z=z)
            candidates.append(self._make_candidate(row, side="long", confidence=_conf(z), md=md, ts=ts))

        for sym, z in short_picks:
            row = sym_to_row[sym]
            md = self._build_metadata(sym, scores, side="short", composite_z=z)
            # Negative z ⇒ p<0.5 ⇒ low confidence. We rebuild from |z| so
            # the conviction reflects the magnitude of the bottom pick.
            candidates.append(
                self._make_candidate(row, side="short", confidence=_conf(abs(z)), md=md, ts=ts)
            )

        if candidates:
            logger.info(
                "factor_sleeve | emitted %d candidates (long=%d short=%d) over universe=%d",
                len(candidates),
                len(long_picks),
                len(short_picks),
                len(universe),
            )
        return candidates, scores

    # ── helpers ─────────────────────────────────────────────────────────────

    def _build_metadata(
        self,
        symbol: str,
        scores: FactorScores,
        *,
        side: str,
        composite_z: float,
    ) -> dict[str, object]:
        family_breakdown = {fam: vals.get(symbol, 0.0) for fam, vals in scores.by_family.items()}
        md: dict[str, object] = {
            "factor_sleeve": True,
            "factor_composite_z": float(composite_z),
            "factor_family_breakdown": family_breakdown,
            "factor_side": side,
        }
        return md

    def _make_candidate(
        self,
        row: FactorUniverseInput,
        *,
        side: str,
        confidence: Decimal,
        md: dict[str, object],
        ts: datetime,
    ) -> SignalCandidate:
        ac = (row.asset_class or "other").strip().lower()
        if ac not in ("equity", "etf", "bond", "forex", "crypto", "future", "option", "other"):
            ac = "other"
        side_typed: Side = "long" if side == "long" else "short"
        c = clip_decimal(confidence, Decimal("0"), Decimal("1"))
        return SignalCandidate(
            symbol=row.symbol,
            asset_class=ac,  # type: ignore[arg-type]
            side=side_typed,
            timestamp=ts,
            raw_signal_strength=c,
            adjusted_signal_strength=c,
            confidence=c,
            strategy_name=self.config.strategy_name,
            metadata=md,
        )
