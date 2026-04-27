"""
signals/factor_scoring.py
==========================
Wave 3 — cross-sectional ranking, group neutralisation, and composite
factor blending.

Inputs: a per-symbol dict of factor → value (some values may be
``None``). Outputs: a per-symbol composite z-score that downstream
``strategies/factor_sleeve.py`` turns into ``SignalCandidate``s.

Conventions:

- Higher is better for every factor. Functions whose raw direction is
  "lower is better" (leverage, realised vol, accruals, reversal_1m)
  apply a negative weight in the family configuration; the scorer
  itself does not flip signs.
- Rank uses a stable z-score (mean / population stddev) computed
  *within each group* when neutralising. Symbols missing a factor are
  left out of that factor's mean/std and contribute zero to the
  composite via ``treat_missing="zero"`` (default) or are excluded
  entirely via ``treat_missing="drop"``.
- All math is float; the sleeve converts the final score to ``Decimal``
  for the SignalCandidate.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterable, Mapping, Optional


# ── factor blend config ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class FactorWeight:
    """One factor's contribution to a family (or the composite)."""

    name: str
    weight: float = 1.0  # negative ⇒ "lower-is-better"

    def signed_weight(self) -> float:
        return float(self.weight)


@dataclass(frozen=True)
class FactorFamily:
    """A weighted collection of factors that share an interpretation."""

    name: str
    members: tuple[FactorWeight, ...] = ()
    weight: float = 1.0  # family weight inside the composite


@dataclass(frozen=True)
class FactorBlend:
    families: tuple[FactorFamily, ...] = ()


# Default blend covers the five families in the plan. The numeric
# weights are deliberately conservative starting points; the operator
# tunes them via ``config/factor_sleeve.yaml``.
DEFAULT_BLEND: FactorBlend = FactorBlend(
    families=(
        FactorFamily(
            name="value",
            weight=1.0,
            members=(
                FactorWeight("earnings_yield", 1.0),
                FactorWeight("book_to_market", 1.0),
                FactorWeight("fcf_yield", 1.0),
                FactorWeight("sales_yield", 0.5),
            ),
        ),
        FactorFamily(
            name="quality",
            weight=1.0,
            members=(
                FactorWeight("profitability", 1.0),
                FactorWeight("margin_stability", 0.5),
                FactorWeight("leverage", -1.0),  # lower is better
                FactorWeight("accruals_proxy", -1.0),
            ),
        ),
        FactorFamily(
            name="momentum",
            weight=1.0,
            members=(
                FactorWeight("momentum_12_1", 1.0),
                FactorWeight("momentum_6m", 0.5),
                FactorWeight("reversal_1m", -0.5),
            ),
        ),
        FactorFamily(
            name="defensive",
            weight=0.7,
            members=(
                FactorWeight("realised_vol", -1.0),
                FactorWeight("downside_vol", -0.5),
                FactorWeight("drawdown_stability", 1.0),
            ),
        ),
        FactorFamily(
            name="carry",
            weight=0.5,
            members=(
                FactorWeight("dividend_yield", 1.0),
                FactorWeight("fx_carry", 1.0),
                FactorWeight("crypto_funding_carry", 1.0),
                FactorWeight("bond_yield_carry", 1.0),
            ),
        ),
    )
)


# ── cross-sectional ranking ─────────────────────────────────────────────────


def rank_cross_section(
    factor_values: Mapping[str, Optional[float]],
) -> dict[str, Optional[float]]:
    """
    Z-score each symbol's factor value across the cross-section.

    Symbols with ``None`` keep ``None`` in the output (they did not
    contribute to the mean/std).
    """
    finite_items = [(k, float(v)) for k, v in factor_values.items() if v is not None and math.isfinite(float(v))]
    if not finite_items:
        return {k: None for k in factor_values}
    vals = [v for _, v in finite_items]
    mean = sum(vals) / len(vals)
    if len(vals) == 1:
        # Only one observation — z = 0 (no spread).
        out = {k: None for k in factor_values}
        out[finite_items[0][0]] = 0.0
        return out
    var = sum((v - mean) ** 2 for v in vals) / (len(vals) - 1)
    sd = math.sqrt(var) if var > 0 else 0.0
    if sd == 0:
        return {k: (0.0 if v is not None and math.isfinite(float(v)) else None) for k, v in factor_values.items()}
    out: dict[str, Optional[float]] = {k: None for k in factor_values}
    for k, v in finite_items:
        out[k] = (v - mean) / sd
    return out


def neutralise_by_group(
    factor_values: Mapping[str, Optional[float]],
    *,
    groups: Mapping[str, str],
) -> dict[str, Optional[float]]:
    """
    Subtract the group mean from each symbol's value (e.g. asset class
    neutralisation). Symbols whose group has fewer than 2 finite values
    are returned unchanged.
    """
    by_group: dict[str, list[float]] = {}
    for sym, v in factor_values.items():
        if v is None or not math.isfinite(float(v)):
            continue
        g = groups.get(sym, "_default")
        by_group.setdefault(g, []).append(float(v))
    means: dict[str, float] = {
        g: (sum(vs) / len(vs)) for g, vs in by_group.items() if len(vs) >= 2
    }
    out: dict[str, Optional[float]] = {}
    for sym, v in factor_values.items():
        if v is None or not math.isfinite(float(v)):
            out[sym] = None
            continue
        g = groups.get(sym, "_default")
        m = means.get(g)
        if m is None:
            out[sym] = float(v)
        else:
            out[sym] = float(v) - m
    return out


# ── composite scoring ───────────────────────────────────────────────────────


@dataclass
class FactorScores:
    """Per-symbol composite score plus a per-family breakdown."""

    composite: dict[str, float] = field(default_factory=dict)
    by_family: dict[str, dict[str, float]] = field(default_factory=dict)

    def top_n(self, n: int) -> list[tuple[str, float]]:
        return sorted(self.composite.items(), key=lambda kv: kv[1], reverse=True)[:n]

    def bottom_n(self, n: int) -> list[tuple[str, float]]:
        return sorted(self.composite.items(), key=lambda kv: kv[1])[:n]


def composite_factor_score(
    *,
    per_symbol_factors: Mapping[str, Mapping[str, Optional[float]]],
    blend: FactorBlend = DEFAULT_BLEND,
    groups: Optional[Mapping[str, str]] = None,
    treat_missing: str = "zero",
) -> FactorScores:
    """
    Build a composite score across all symbols.

    Steps for each factor:
      1. Optional neutralisation by ``groups`` (asset class).
      2. Cross-sectional z-score.
      3. Multiplied by the factor's signed weight inside its family.
      4. Family-weighted sum becomes the composite z-score.

    ``treat_missing``:
      - ``"zero"`` (default): missing factor values contribute 0.
      - ``"drop"``: any symbol missing *any* factor in the blend gets
        ``None`` and is excluded from the result.
    """
    if treat_missing not in ("zero", "drop"):
        raise ValueError("treat_missing must be 'zero' or 'drop'")

    symbols = list(per_symbol_factors.keys())
    by_family: dict[str, dict[str, float]] = {fam.name: {s: 0.0 for s in symbols} for fam in blend.families}
    composite_raw: dict[str, float] = {s: 0.0 for s in symbols}
    drop: set[str] = set()

    for fam in blend.families:
        family_total: dict[str, float] = {s: 0.0 for s in symbols}
        for fw in fam.members:
            # Pull the per-symbol factor values into a dict.
            raw = {s: per_symbol_factors[s].get(fw.name) for s in symbols}
            if groups:
                raw = neutralise_by_group(raw, groups=groups)
            ranked = rank_cross_section(raw)
            for s in symbols:
                z = ranked.get(s)
                if z is None:
                    if treat_missing == "drop":
                        drop.add(s)
                    # else: contribute 0
                    continue
                family_total[s] += fw.signed_weight() * float(z)
        for s in symbols:
            by_family[fam.name][s] = family_total[s]
            composite_raw[s] += fam.weight * family_total[s]

    if treat_missing == "drop":
        for s in drop:
            composite_raw[s] = float("nan")

    composite = {s: v for s, v in composite_raw.items() if math.isfinite(v)}
    by_family_out = {
        fam: {s: v for s, v in vals.items() if s in composite}
        for fam, vals in by_family.items()
    }
    return FactorScores(composite=composite, by_family=by_family_out)


# ── YAML config helpers ─────────────────────────────────────────────────────


def blend_from_config(raw: Optional[Mapping[str, object]]) -> FactorBlend:
    """Parse a YAML-style dict into a ``FactorBlend``."""
    if not raw:
        return DEFAULT_BLEND
    fams_raw = raw.get("families")  # type: ignore[union-attr]
    if not isinstance(fams_raw, Iterable):
        return DEFAULT_BLEND
    families: list[FactorFamily] = []
    for fam in fams_raw:
        if not isinstance(fam, Mapping):
            continue
        name = str(fam.get("name") or "")
        if not name:
            continue
        members = tuple(
            FactorWeight(name=str(m["name"]), weight=float(m.get("weight", 1.0)))
            for m in (fam.get("members") or [])
            if isinstance(m, Mapping) and m.get("name")
        )
        families.append(
            FactorFamily(
                name=name,
                members=members,
                weight=float(fam.get("weight", 1.0)),
            )
        )
    return FactorBlend(families=tuple(families)) if families else DEFAULT_BLEND
