from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class IntelligenceTier(str, Enum):
    """Four-tier model for universe intelligence (distinct from core/scan/light JSON)."""

    CANDIDATE = "candidate"
    COLD_SCAN = "cold_scan"
    ACTIVE = "active"
    CORE = "core"


@dataclass
class TierAssignment:
    symbol: str
    tier: IntelligenceTier
    representative_for: str | None = None
    pair_watch: bool = False
    notes: str = ""


@dataclass
class UniverseIntelligenceState:
    """Persisted snapshot shape (subset merged into API)."""

    candidate_count: int = 0
    cold_scan: list[str] = field(default_factory=list)
    active_eval: list[str] = field(default_factory=list)
    core: list[str] = field(default_factory=list)
    clusters: list[dict[str, Any]] = field(default_factory=list)
    promotions: list[dict[str, Any]] = field(default_factory=list)
    last_full_cluster_at: str | None = None
    version: int = 1

    def to_json_obj(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "candidate_count": self.candidate_count,
            "cold_scan": list(self.cold_scan),
            "active_eval": list(self.active_eval),
            "core": list(self.core),
            "clusters": list(self.clusters),
            "promotions": list(self.promotions),
            "last_full_cluster_at": self.last_full_cluster_at,
        }

    @classmethod
    def from_json_obj(cls, raw: dict[str, Any]) -> UniverseIntelligenceState:
        return cls(
            version=int(raw.get("version") or 1),
            candidate_count=int(raw.get("candidate_count") or 0),
            cold_scan=[str(x).upper() for x in raw.get("cold_scan") or [] if str(x).strip()],
            active_eval=[str(x).upper() for x in raw.get("active_eval") or [] if str(x).strip()],
            core=[str(x).upper() for x in raw.get("core") or [] if str(x).strip()],
            clusters=list(raw.get("clusters") or []),
            promotions=list(raw.get("promotions") or []),
            last_full_cluster_at=raw.get("last_full_cluster_at"),
        )
