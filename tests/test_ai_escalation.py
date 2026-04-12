"""Unit tests for ai/escalation.py (pure functions)."""

from __future__ import annotations

import pytest

from ai.escalation import (
    compute_escalation_score,
    should_escalate_to_local_llm,
    should_escalate_to_premium,
)
from ai.schemas import EscalationContext, ProviderResult


def _ctx(**kwargs) -> EscalationContext:
    base = {
        "rules_result": None,
        "merged_confidence": 0.5,
        "materiality": "medium",
        "novelty_score": 0.0,
        "provider_disagreement": 0.0,
        "llm_disagreement": 0.0,
        "headline": "Test headline",
        "source": "reuters",
    }
    base.update(kwargs)
    return EscalationContext(**base)


def test_compute_escalation_score_in_range() -> None:
    ctx = _ctx(merged_confidence=0.5, novelty_score=0.5, provider_disagreement=0.2, llm_disagreement=0.1)
    s = compute_escalation_score(ctx)
    assert 0.0 <= s <= 1.0


def test_compute_escalation_score_high_ambiguity() -> None:
    ctx = _ctx(merged_confidence=0.0, materiality="high", novelty_score=0.0)
    s = compute_escalation_score(ctx)
    assert s > 0.3


@pytest.mark.parametrize(
    "materiality,merged_conf,expect",
    [
        ("high", 0.99, True),
        ("medium", 0.99, False),
        ("low", 0.3, True),
        ("low", 0.9, False),
    ],
)
def test_should_escalate_to_local_llm(materiality: str, merged_conf: float, expect: bool) -> None:
    ctx = _ctx(materiality=materiality, merged_confidence=merged_conf, provider_disagreement=0.0)
    assert should_escalate_to_local_llm(ctx) is expect


def test_should_escalate_to_local_llm_skips_duplicate() -> None:
    dup = ProviderResult(provider_name="rules", is_duplicate=True)
    ctx = _ctx(rules_result=dup, materiality="high", merged_confidence=0.0)
    assert should_escalate_to_local_llm(ctx) is False


def test_should_escalate_to_premium_disabled() -> None:
    ctx = _ctx(merged_confidence=0.0)
    assert should_escalate_to_premium(ctx, fallback_enabled=False) is False


def test_should_escalate_to_premium_emergency_keyword() -> None:
    ctx = _ctx(headline="Breaking: FOMC emergency rate cut", merged_confidence=0.99)
    assert (
        should_escalate_to_premium(
            ctx,
            fallback_enabled=True,
            emergency_keywords=["emergency"],
            escalation_threshold=0.99,
        )
        is True
    )


def test_should_escalate_to_premium_score_threshold() -> None:
    ctx = _ctx(
        merged_confidence=0.2,
        materiality="high",
        novelty_score=0.8,
        provider_disagreement=0.5,
        llm_disagreement=0.5,
        headline="volatile",
    )
    assert should_escalate_to_premium(ctx, fallback_enabled=True, escalation_threshold=0.3) is True
