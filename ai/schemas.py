"""
ai/schemas.py
=============
Shared data types for the local-first AI provider architecture.
Used by providers, the router, and the escalation engine.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ProviderResult:
    """Result from a single AI provider (may be partial)."""

    provider_name: str

    sentiment: float | None = None
    confidence: float | None = None
    directional_bias: str | None = None
    affected_symbols: list[str] = field(default_factory=list)
    event_type: str | None = None
    decay_hours: int | None = None
    rationale: str | None = None

    materiality: str = "medium"
    novelty_score: float = 0.0
    is_duplicate: bool = False
    source_credibility: str = "medium"

    latency_ms: int = 0
    cost_estimate_gbp: float = 0.0
    success: bool = True
    error: str | None = None


@dataclass
class EscalationContext:
    """Aggregated context used to decide whether to escalate to a costlier provider."""

    rules_result: ProviderResult | None = None
    sentiment_result: ProviderResult | None = None
    local_llm_result: ProviderResult | None = None
    local_llm_secondary: ProviderResult | None = None
    merged_confidence: float = 0.0
    materiality: str = "medium"
    novelty_score: float = 0.0
    provider_disagreement: float = 0.0
    llm_disagreement: float = 0.0
    headline: str = ""
    source: str = ""


@dataclass
class EnsembleVerdict:
    """Result of comparing two local LLM outputs."""

    outcome: str  # "agree" | "soft_disagree" | "hard_disagree" | "single"
    merged_sentiment: float = 0.0
    merged_confidence: float = 0.0
    merged_bias: str = "neutral"
    disagreement: float = 0.0
    winner: ProviderResult | None = None
    rationale: str = ""


# ── AI task identifiers (narrow, explicit, never vague) ─────────────────────
AI_TASKS = frozenset({
    "headline_sentiment",
    "event_classification",
    "asset_relevance",
    "trade_rationale",
    "macro_regime_assist",
})
