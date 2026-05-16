"""
system/fusion_weights.py
==========================
Phase F — learned, regime-conditional fusion weights (SHADOW ONLY).

Today the opportunity score blends 7 components (momentum, volume_anomaly,
news_impact, regime_alignment, liquidity_quality, structure_quality,
relative_strength) with STATIC weights from allocation.yaml. Phase F learns
a per-regime weight vector over those same components and exposes a
*shadow* score so we can measure — offline, on real forward returns —
whether learned regime-conditional weighting actually ranks opportunities
better than the static blend, BEFORE it is ever allowed to influence
sizing.

Discipline (same as Phases C/D/E):
  * Inert by default — no artifact / flag off ⇒ ``shadow_score`` returns
    None and nothing changes.
  * Governed — an artifact must carry ``promote_eligible: True`` (set only
    by the evidence report when learned IC beats static IC) before any
    future code path may use it live. ``load`` refuses otherwise.
  * Read-only in the hot path — the caller only stamps the shadow score
    into metadata; the live ``opportunity_score`` is never altered here.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

COMPONENTS = (
    "momentum",
    "volume_anomaly",
    "news_impact",
    "regime_alignment",
    "liquidity_quality",
    "structure_quality",
    "relative_strength",
)

DEFAULT_ARTIFACT = Path(
    "reports/models/phase_f_fusion_weights/latest_phase_f_fusion_weights.json"
)


def fusion_weights_shadow_enabled() -> bool:
    v = os.getenv("FUSION_WEIGHTS_SHADOW", "").strip().lower()
    return v in ("1", "true", "yes", "on")


@dataclass
class RegimeConditionalFusionWeights:
    """Per-regime non-negative weight vectors over ``COMPONENTS``.

    ``by_regime[regime] = {component: weight}``; ``default`` is the
    fallback when the live regime is absent from the learned set.
    """

    by_regime: dict[str, dict[str, float]] = field(default_factory=dict)
    default: dict[str, float] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    # ── shadow scoring (pure, never raises) ──────────────────────────
    def shadow_score(
        self, components: dict[str, float], regime_label: str
    ) -> float | None:
        """Alternative opportunity score in [0,1] from learned weights, or
        None if this weight set is unusable (caller then skips safely)."""
        try:
            w = self.by_regime.get(str(regime_label)) or self.default
            if not w:
                return None
            num = 0.0
            den = 0.0
            for c in COMPONENTS:
                wc = float(w.get(c, 0.0) or 0.0)
                if wc <= 0.0:
                    continue
                num += wc * float(components.get(c, 0.0) or 0.0)
                den += wc
            if den <= 0.0:
                return None
            s = num / den
            return 0.0 if s < 0.0 else (1.0 if s > 1.0 else s)
        except Exception:  # noqa: BLE001 — shadow must never disturb live
            return None

    # ── persistence (governed) ───────────────────────────────────────
    def to_dict(self) -> dict[str, Any]:
        return {
            "by_regime": self.by_regime,
            "default": self.default,
            "metadata": self.metadata,
        }

    def save(self, path: Path | str = DEFAULT_ARTIFACT) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")

    @staticmethod
    def load(
        path: Path | str = DEFAULT_ARTIFACT, *, require_promote: bool = True
    ) -> "RegimeConditionalFusionWeights | None":
        """Load the artifact, or None if absent/unusable. When
        ``require_promote`` (the live-path default), refuses any artifact
        not explicitly marked ``promote_eligible: True`` by the evidence
        report — config/edits alone can never activate it."""
        p = Path(path)
        if not p.exists():
            return None
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return None
        meta = raw.get("metadata") or {}
        if require_promote and meta.get("promote_eligible") is not True:
            return None
        return RegimeConditionalFusionWeights(
            by_regime=dict(raw.get("by_regime") or {}),
            default=dict(raw.get("default") or {}),
            metadata=dict(meta),
        )
