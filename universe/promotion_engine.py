from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass
class PromotionCandidate:
    symbol: str
    volume_z: float
    price_z: float
    news_shock: bool
    corr_break_z: float
    funding_bps: float | None
    spread_div_bps: float


@dataclass
class PromotionDecision:
    promote: bool
    reason: str
    tier_hint: str = "cold_scan"
    expires_at_iso: str | None = None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def evaluate_promotion(
    c: PromotionCandidate,
    rules: dict[str, Any],
) -> PromotionDecision:
    """Rule-based promotion from cold scan; no ML."""
    vz_th = float(rules.get("volume_z_threshold", 3.0))
    pz_th = float(rules.get("price_z_threshold", 3.0))
    cb_th = float(rules.get("correlation_break_z", 2.5))
    fund_th = float(rules.get("funding_opportunity_bps", 15.0))
    spr_th = float(rules.get("spread_divergence_bps", 8.0))
    ttl_min = int(rules.get("promotion_ttl_minutes", 240))

    reasons: list[str] = []
    if c.volume_z >= vz_th:
        reasons.append("volume_z")
    if abs(c.price_z) >= pz_th:
        reasons.append("price_z")
    if c.news_shock:
        reasons.append("news_shock")
    if c.corr_break_z >= cb_th:
        reasons.append("correlation_break")
    if c.funding_bps is not None and abs(c.funding_bps) >= fund_th:
        reasons.append("funding")
    if c.spread_div_bps >= spr_th:
        reasons.append("spread_divergence")

    if not reasons:
        return PromotionDecision(False, "below_thresholds")

    until = _utc_now().timestamp() + ttl_min * 60
    exp = datetime.fromtimestamp(until, tz=timezone.utc).isoformat()
    return PromotionDecision(True, "+".join(reasons), "promoted", exp)


@dataclass
class DemotionState:
    promoted_at: dict[str, str] = field(default_factory=dict)

    def should_demote(
        self,
        symbol: str,
        *,
        signal_gone: bool,
        redundancy_score: float,
        redundancy_threshold: float = 0.85,
    ) -> bool:
        if signal_gone:
            return True
        return redundancy_score >= redundancy_threshold
