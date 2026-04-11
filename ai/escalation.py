"""
ai/escalation.py
================
Necessity-based escalation engine.

Decides whether a headline warrants a more expensive provider based on:
- ambiguity (low local confidence)
- materiality (event importance)
- novelty (unusual / unseen pattern)
- provider disagreement (rules vs FinBERT conflict)
- source credibility

NO hard daily call caps. The escalation criteria themselves are the limiter.
Thresholds start as configurable defaults and should evolve into dynamic
parameters managed by the ParameterManager with regime/exposure overrides.
"""

from __future__ import annotations

from typing import Any

from loguru import logger

from ai.schemas import EscalationContext, ProviderResult


_MATERIALITY_SCORES = {"low": 0.15, "medium": 0.5, "high": 1.0}
_CREDIBILITY_MULTIPLIER = {"low": 0.6, "medium": 1.0, "high": 1.0}


def compute_escalation_score(ctx: EscalationContext, weights: dict[str, float] | None = None) -> float:
    """
    Compute a 0..1 escalation score.  Higher = stronger case for premium escalation.

    The weights are starting heuristics — intentionally not sacred constants.
    They should eventually be learned or tuned via ParameterManager.
    """
    w = weights or {
        "ambiguity": 0.35,
        "materiality": 0.30,
        "novelty": 0.20,
        "disagreement": 0.15,
    }

    ambiguity = 1.0 - max(0.0, min(1.0, ctx.merged_confidence))
    mat = _MATERIALITY_SCORES.get(ctx.materiality, 0.5)
    novelty = max(0.0, min(1.0, ctx.novelty_score))
    disagree = max(0.0, min(1.0, ctx.provider_disagreement))

    cred = "medium"
    if ctx.rules_result is not None:
        cred = ctx.rules_result.source_credibility
    cred_mult = _CREDIBILITY_MULTIPLIER.get(cred, 1.0)

    score = (
        w.get("ambiguity", 0.35) * ambiguity
        + w.get("materiality", 0.30) * mat
        + w.get("novelty", 0.20) * novelty
        + w.get("disagreement", 0.15) * disagree
    ) * cred_mult

    return min(1.0, max(0.0, round(score, 4)))


def should_escalate_to_local_llm(
    ctx: EscalationContext,
    *,
    min_confidence: float = 0.55,
) -> bool:
    """
    Should we send this item to the local LLM?
    Triggered when rules + FinBERT result is insufficient.
    """
    if ctx.rules_result is not None and ctx.rules_result.is_duplicate:
        return False
    if ctx.merged_confidence >= min_confidence and ctx.provider_disagreement < 0.3:
        return False
    return True


def should_escalate_to_premium(
    ctx: EscalationContext,
    *,
    escalation_threshold: float = 0.55,
    weights: dict[str, float] | None = None,
    emergency_keywords: list[str] | None = None,
    fallback_enabled: bool = False,
) -> bool:
    """
    Should we escalate to the premium (paid) provider?

    Criteria:
    1. Premium must be enabled at all
    2. The headline is NOT a duplicate
    3. Local providers are NOT in agreement with high confidence
    4. The event is materially important enough to justify the cost
    5. OR it matches an emergency keyword (always escalate)

    No daily call caps. The criteria themselves are the limiter.
    """
    if not fallback_enabled:
        return False

    if ctx.rules_result is not None and ctx.rules_result.is_duplicate:
        return False

    ek = emergency_keywords or []
    headline_lower = ctx.headline.lower()
    if any(kw in headline_lower for kw in ek):
        logger.info("escalation | emergency keyword match — escalating | headline={}", ctx.headline[:80])
        return True

    score = compute_escalation_score(ctx, weights)
    if score >= escalation_threshold:
        logger.info(
            "escalation | score {:.3f} >= threshold {:.3f} — escalating | headline={}",
            score, escalation_threshold, ctx.headline[:80],
        )
        return True

    return False


def compute_provider_disagreement(
    rules: ProviderResult | None,
    sentiment: ProviderResult | None,
) -> float:
    """
    Measure how much rules and FinBERT disagree on sentiment direction.
    Returns 0..1 where 1 = maximum disagreement (one bullish, one bearish).
    """
    if rules is None or sentiment is None:
        return 0.0
    r_sent = rules.sentiment or 0.0
    s_sent = sentiment.sentiment or 0.0
    if r_sent == 0.0 or s_sent == 0.0:
        return 0.0
    if (r_sent > 0 and s_sent < 0) or (r_sent < 0 and s_sent > 0):
        return min(1.0, abs(r_sent - s_sent) / 2.0)
    return 0.0
