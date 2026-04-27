"""
ai/fusion.py
==============
Wave 7 — multimodal fusion.

Combines structured-forecast / news / macro / graph / accumulator /
execution / portfolio signals into a single ``FusionScore`` per
symbol that downstream layers (signal accumulator, opportunity engine,
dashboard) can consume.

Hard constraints (verified by ``tests/test_wave7_fusion.py``):

- This module imports nothing from ``brokers.*`` and never calls an
  LLM. The LLM stays in the classification / explanation role inside
  ``ai/router.py`` and ``ai/pipeline.py``.
- ``FusionScore`` is decomposable: ``contributions`` records every
  source's per-component bias and weight, so the dashboard can render
  the "why".
- Conflicting sources reduce confidence: when sources disagree on
  direction, ``conflict_score`` rises and ``confidence`` falls.
- ``trigger_llm_ensemble`` is a *recommendation* the operator's
  escalation chain (``ai/escalation.py``) can read; this module never
  invokes the LLM directly.

Operator workflow:

  1. Build ``MarketContext`` per symbol via ``ai.market_context.MarketContextBuilder``.
  2. Call ``MultimodalFusion(config).combine(context)``.
  3. Read ``score.directional_bias`` for the direction, ``score.confidence``
     for the conviction, ``score.contributions`` for the audit trail.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Mapping, Optional

import yaml

from ai.market_context import MarketContext

logger = logging.getLogger(__name__)


DEFAULT_CONFIG_PATH = Path("config/multimodal_fusion.yaml")


# ── public dataclasses ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class SourceContribution:
    name: str
    direction: float       # signed contribution to the bias, in [-w, +w]
    weight: float          # |contribution| budget allowed for this source
    rationale: str = ""


@dataclass
class FusionScore:
    symbol: str
    asset_class: str
    directional_bias: float          # signed; roughly [-1, 1]
    confidence: float                # 0..1
    affected_symbols: tuple[str, ...] = ()
    affected_asset_classes: tuple[str, ...] = ()
    freshness_seconds: Optional[float] = None
    conflict_score: float = 0.0       # 0..1; higher = more disagreement
    rationale: str = ""
    contributions: list[SourceContribution] = field(default_factory=list)
    trigger_llm_ensemble: bool = False
    metadata: dict[str, object] = field(default_factory=dict)


# ── config ─────────────────────────────────────────────────────────────────


@dataclass
class FusionWeights:
    """Per-source relative weight in the fusion sum."""

    structured_forecast: float = 1.0
    news: float = 0.7
    macro: float = 0.4
    graph: float = 0.5
    accumulator: float = 0.6
    execution: float = 0.2
    portfolio: float = 0.0  # default 0: portfolio context is for filtering, not directional bias

    @classmethod
    def from_dict(cls, raw: Optional[Mapping[str, object]]) -> "FusionWeights":
        if not raw:
            return cls()
        d = dict(raw)
        return cls(
            structured_forecast=float(d.get("structured_forecast", 1.0)),
            news=float(d.get("news", 0.7)),
            macro=float(d.get("macro", 0.4)),
            graph=float(d.get("graph", 0.5)),
            accumulator=float(d.get("accumulator", 0.6)),
            execution=float(d.get("execution", 0.2)),
            portfolio=float(d.get("portfolio", 0.0)),
        )


@dataclass
class FusionConfig:
    enabled: bool = False
    weights: FusionWeights = field(default_factory=FusionWeights)
    min_sources_for_high_confidence: int = 2
    materiality_llm_threshold: float = 0.7

    @classmethod
    def from_dict(cls, raw: Optional[Mapping[str, object]]) -> "FusionConfig":
        if not raw:
            return cls()
        sect = raw.get("multimodal_fusion") if "multimodal_fusion" in raw else raw  # type: ignore[union-attr]
        sect = dict(sect or {})
        return cls(
            enabled=bool(sect.get("enabled", False)),
            weights=FusionWeights.from_dict(sect.get("weights")),  # type: ignore[arg-type]
            min_sources_for_high_confidence=int(sect.get("min_sources_for_high_confidence", 2)),
            materiality_llm_threshold=float(sect.get("materiality_llm_threshold", 0.7)),
        )

    @classmethod
    def load(cls, path: Path | str = DEFAULT_CONFIG_PATH) -> "FusionConfig":
        p = Path(path)
        if not p.exists():
            return cls()
        try:
            raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise RuntimeError(f"could not parse {p}: {exc}") from exc
        return cls.from_dict(raw)


# ── fusion ─────────────────────────────────────────────────────────────────


def _clip(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, float(value)))


def _signum(x: Optional[float]) -> int:
    if x is None or x == 0 or math.isnan(x):
        return 0
    return 1 if x > 0 else -1


@dataclass
class MultimodalFusion:
    config: FusionConfig = field(default_factory=FusionConfig)

    def combine(self, ctx: MarketContext) -> FusionScore:
        weights = self.config.weights
        contributions: list[SourceContribution] = []

        # --- per-source contributions --------------------------------------

        if ctx.structured_forecast is not None:
            sf = ctx.structured_forecast
            er = sf.expected_return
            conf = sf.confidence if sf.confidence is not None else 0.5
            if er is not None and weights.structured_forecast > 0:
                # Squash expected return into [-1, 1] via tanh so a 5% forecast
                # does not blow past a 1% forecast linearly.
                squashed = math.tanh(float(er) * 20.0)
                contrib = squashed * float(conf) * weights.structured_forecast
                contributions.append(
                    SourceContribution(
                        name="structured_forecast",
                        direction=contrib,
                        weight=weights.structured_forecast,
                        rationale=f"er={er:+.4f} conf={conf:.2f}",
                    )
                )

        if ctx.news is not None and weights.news > 0:
            score = ctx.news.score if ctx.news.score is not None else 0.0
            mat = ctx.news.materiality if ctx.news.materiality is not None else 0.5
            if score != 0.0 or mat != 0.0:
                contrib = float(score) * float(max(0.0, min(1.0, mat))) * weights.news
                contributions.append(
                    SourceContribution(
                        name="news",
                        direction=contrib,
                        weight=weights.news,
                        rationale=f"score={score:+.2f} mat={mat:.2f}",
                    )
                )

        if ctx.macro is not None and weights.macro > 0:
            r = ctx.macro.regime_score
            if r is not None:
                squashed = math.tanh(float(r))
                contrib = squashed * weights.macro
                contributions.append(
                    SourceContribution(
                        name="macro",
                        direction=contrib,
                        weight=weights.macro,
                        rationale=f"regime={ctx.macro.regime_label} score={r:+.2f}",
                    )
                )

        if ctx.graph is not None and weights.graph > 0 and ctx.graph.propagation_strength is not None:
            sign_dir = ctx.graph.propagation_strength
            contrib = float(sign_dir) * weights.graph
            contributions.append(
                SourceContribution(
                    name="graph",
                    direction=contrib,
                    weight=weights.graph,
                    rationale=f"upstream={ctx.graph.upstream_trigger} strength={sign_dir:+.2f}",
                )
            )

        if ctx.accumulator is not None and weights.accumulator > 0:
            ac_score = ctx.accumulator.score
            ac_conf = ctx.accumulator.confidence if ctx.accumulator.confidence is not None else 0.5
            if ac_score is not None:
                squashed = math.tanh(float(ac_score) * 2.0)
                contrib = squashed * float(ac_conf) * weights.accumulator
                contributions.append(
                    SourceContribution(
                        name="accumulator",
                        direction=contrib,
                        weight=weights.accumulator,
                        rationale=f"score={ac_score:+.2f} conf={ac_conf:.2f}",
                    )
                )

        if ctx.execution is not None and weights.execution > 0:
            # Execution feedback contributes a *negative* bias when slippage is
            # high — high cost makes the trade marginally less attractive.
            ec = ctx.execution
            slip = ec.last_slippage_bps if ec.last_slippage_bps is not None else 0.0
            if slip > 0:
                # Map 0..50 bps onto 0..1 then negate.
                penalty = -min(1.0, slip / 50.0)
                contrib = penalty * weights.execution
                contributions.append(
                    SourceContribution(
                        name="execution",
                        direction=contrib,
                        weight=weights.execution,
                        rationale=f"slip={slip:.1f}bps",
                    )
                )

        # --- aggregation ---------------------------------------------------

        total_weight = float(sum(c.weight for c in contributions))
        if total_weight <= 0 or not contributions:
            return FusionScore(
                symbol=ctx.symbol,
                asset_class=ctx.asset_class,
                directional_bias=0.0,
                confidence=0.0,
                rationale="no_sources",
            )

        raw_bias = sum(c.direction for c in contributions) / total_weight
        directional_bias = _clip(raw_bias, -1.0, 1.0)

        # Conflict: |sum / Σ|contributions| | close to 1 ⇒ aligned, close to 0 ⇒ conflict.
        sum_abs = float(sum(abs(c.direction) for c in contributions))
        alignment = (
            abs(sum(c.direction for c in contributions)) / sum_abs
            if sum_abs > 0
            else 0.0
        )
        conflict_score = _clip(1.0 - alignment, 0.0, 1.0)

        # Confidence: bias magnitude × alignment × source-count factor.
        n_sources = len(contributions)
        n_factor = min(1.0, n_sources / max(1, self.config.min_sources_for_high_confidence))
        confidence = _clip(abs(directional_bias) * alignment * n_factor, 0.0, 1.0)

        # LLM ensemble trigger: any high-materiality news in the context.
        trigger_llm = False
        if ctx.news is not None and ctx.news.materiality is not None:
            trigger_llm = float(ctx.news.materiality) >= self.config.materiality_llm_threshold

        # Affected universe — union of the directly-named symbol and the
        # graph context's related symbols.
        affected = (ctx.symbol,) + tuple(ctx.graph.related_symbols if ctx.graph else ())
        affected_asset_classes = tuple(
            ctx.graph.affected_asset_classes if ctx.graph else (ctx.asset_class,)
        )

        rationale = "; ".join(
            f"{c.name}={c.direction:+.3f}" for c in contributions
        )

        meta: dict[str, object] = {
            "fusion_total_weight": total_weight,
            "fusion_alignment": alignment,
            "fusion_n_sources": n_sources,
        }

        return FusionScore(
            symbol=ctx.symbol,
            asset_class=ctx.asset_class,
            directional_bias=directional_bias,
            confidence=confidence,
            affected_symbols=affected,
            affected_asset_classes=affected_asset_classes,
            freshness_seconds=None,  # caller stamps this if it has the wall-clock anchor
            conflict_score=conflict_score,
            rationale=rationale,
            contributions=contributions,
            trigger_llm_ensemble=trigger_llm,
            metadata=meta,
        )

    def combine_many(self, contexts: Iterable[MarketContext]) -> list[FusionScore]:
        return [self.combine(c) for c in contexts]
