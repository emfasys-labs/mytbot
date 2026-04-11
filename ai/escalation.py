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

from ai.schemas import EnsembleVerdict, EscalationContext, ProviderResult


_MATERIALITY_SCORES = {"low": 0.15, "medium": 0.5, "high": 1.0}
_CREDIBILITY_MULTIPLIER = {"low": 0.6, "medium": 1.0, "high": 1.0}

_BIAS_DIRECTION = {"bullish": 1, "bearish": -1, "neutral": 0}


def compute_escalation_score(ctx: EscalationContext, weights: dict[str, float] | None = None) -> float:
    """
    Compute a 0..1 escalation score.  Higher = stronger case for premium escalation.

    The weights are starting heuristics — intentionally not sacred constants.
    They should eventually be learned or tuned via ParameterManager.
    """
    w = weights or {
        "ambiguity": 0.25,
        "materiality": 0.25,
        "novelty": 0.15,
        "disagreement": 0.10,
        "llm_disagreement": 0.25,
    }

    ambiguity = 1.0 - max(0.0, min(1.0, ctx.merged_confidence))
    mat = _MATERIALITY_SCORES.get(ctx.materiality, 0.5)
    novelty = max(0.0, min(1.0, ctx.novelty_score))
    disagree = max(0.0, min(1.0, ctx.provider_disagreement))
    llm_disagree = max(0.0, min(1.0, ctx.llm_disagreement))

    cred = "medium"
    if ctx.rules_result is not None:
        cred = ctx.rules_result.source_credibility
    cred_mult = _CREDIBILITY_MULTIPLIER.get(cred, 1.0)

    score = (
        w.get("ambiguity", 0.25) * ambiguity
        + w.get("materiality", 0.25) * mat
        + w.get("novelty", 0.15) * novelty
        + w.get("disagreement", 0.10) * disagree
        + w.get("llm_disagreement", 0.25) * llm_disagree
    ) * cred_mult

    return min(1.0, max(0.0, round(score, 4)))


def should_escalate_to_local_llm(
    ctx: EscalationContext,
    *,
    min_confidence: float = 0.55,
    min_confidence_medium: float = 0.75,
    always_escalate_high_materiality: bool = True,
) -> bool:
    """
    Should we send this item to the local LLM ensemble?

    Materiality-aware gating:
        HIGH materiality  — ALWAYS escalate (macro, geopolitical, M&A).
                            FinBERT is not trusted alone on portfolio-moving events.
        MEDIUM materiality — escalate if FinBERT confidence < 0.75 OR providers disagree.
        LOW materiality    — escalate only if FinBERT confidence < 0.55 (original behavior).
    """
    if ctx.rules_result is not None and ctx.rules_result.is_duplicate:
        return False

    materiality = ctx.materiality

    if materiality == "high" and always_escalate_high_materiality:
        return True

    if materiality == "medium":
        if ctx.merged_confidence >= min_confidence_medium and ctx.provider_disagreement < 0.2:
            return False
        return True

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


def evaluate_ensemble(
    primary: ProviderResult,
    secondary: ProviderResult | None,
    *,
    hard_disagree_threshold: float = 0.4,
    confidence_boost: float = 0.15,
) -> EnsembleVerdict:
    """Compare two local LLM results and produce a consensus verdict.

    Outcomes:
        agree          — same direction, both reasonably confident → boost confidence
        soft_disagree  — same direction but weak, or mixed neutral → average
        hard_disagree  — opposite directions with conviction → must escalate
        single         — only one model available or one failed
    """
    if secondary is None or not secondary.success:
        return EnsembleVerdict(
            outcome="single",
            merged_sentiment=primary.sentiment or 0.0,
            merged_confidence=primary.confidence or 0.0,
            merged_bias=primary.directional_bias or "neutral",
            disagreement=0.0,
            winner=primary,
            rationale="single model available",
        )

    if not primary.success:
        return EnsembleVerdict(
            outcome="single",
            merged_sentiment=secondary.sentiment or 0.0,
            merged_confidence=secondary.confidence or 0.0,
            merged_bias=secondary.directional_bias or "neutral",
            disagreement=0.0,
            winner=secondary,
            rationale="primary failed, using secondary",
        )

    p_sent = primary.sentiment or 0.0
    s_sent = secondary.sentiment or 0.0
    p_conf = primary.confidence or 0.0
    s_conf = secondary.confidence or 0.0
    p_dir = _BIAS_DIRECTION.get(primary.directional_bias or "neutral", 0)
    s_dir = _BIAS_DIRECTION.get(secondary.directional_bias or "neutral", 0)

    sent_distance = abs(p_sent - s_sent)
    opposite_directions = (p_dir != 0 and s_dir != 0 and p_dir != s_dir)

    if opposite_directions and min(p_conf, s_conf) > 0.3:
        disagreement = min(1.0, sent_distance / 2.0 + abs(p_conf - s_conf) * 0.3)
        logger.info(
            "ensemble | HARD DISAGREE | primary={:.2f}/{} secondary={:.2f}/{} dist={:.2f}",
            p_sent, primary.directional_bias, s_sent, secondary.directional_bias, disagreement,
        )
        higher = primary if p_conf >= s_conf else secondary
        return EnsembleVerdict(
            outcome="hard_disagree",
            merged_sentiment=(p_sent + s_sent) / 2,
            merged_confidence=min(p_conf, s_conf) * 0.5,
            merged_bias="neutral",
            disagreement=disagreement,
            winner=higher,
            rationale=f"opposite directions: {primary.provider_name}={primary.directional_bias} vs {secondary.provider_name}={secondary.directional_bias}",
        )

    if sent_distance > hard_disagree_threshold and min(p_conf, s_conf) < 0.4:
        avg_sent = (p_sent + s_sent) / 2
        avg_conf = (p_conf + s_conf) / 2
        bias = "bullish" if avg_sent > 0.1 else "bearish" if avg_sent < -0.1 else "neutral"
        return EnsembleVerdict(
            outcome="soft_disagree",
            merged_sentiment=avg_sent,
            merged_confidence=avg_conf,
            merged_bias=bias,
            disagreement=sent_distance,
            winner=primary if p_conf >= s_conf else secondary,
            rationale=f"same direction but weak agreement (dist={sent_distance:.2f})",
        )

    w_p = p_conf / (p_conf + s_conf) if (p_conf + s_conf) > 0 else 0.5
    w_s = 1.0 - w_p
    merged_sent = p_sent * w_p + s_sent * w_s
    merged_conf = min(1.0, max(p_conf, s_conf) + confidence_boost)
    bias = "bullish" if merged_sent > 0.1 else "bearish" if merged_sent < -0.1 else "neutral"

    logger.info(
        "ensemble | AGREE | primary={:.2f}/{:.2f} secondary={:.2f}/{:.2f} → merged={:.2f}/{:.2f}",
        p_sent, p_conf, s_sent, s_conf, merged_sent, merged_conf,
    )
    return EnsembleVerdict(
        outcome="agree",
        merged_sentiment=merged_sent,
        merged_confidence=merged_conf,
        merged_bias=bias,
        disagreement=sent_distance,
        winner=primary,
        rationale=f"models agree: confidence boosted {max(p_conf, s_conf):.2f} → {merged_conf:.2f}",
    )
