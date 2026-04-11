"""
ai/router.py
============
Local-first AI router.  Drop-in replacement for NewsClassifier.

Provider chain:  rules → FinBERT → local LLM → optional premium fallback.
Escalation is necessity-based (no hard daily caps).

Exposes the same interface as NewsClassifier so AIPipeline can use it
without structural changes:
    - score_batch(items: list[NewsItem]) -> list[NewsScore | None]
    - validate_startup() -> bool
    - generate_rationale(signal_context) -> str
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import Any, Optional

from loguru import logger

from ai.escalation import (
    EscalationContext,
    compute_provider_disagreement,
    should_escalate_to_local_llm,
    should_escalate_to_premium,
)
from ai.news_classifier import NewsItem, NewsScore
from ai.providers.fin_sentiment_provider import FinSentimentProvider
from ai.providers.local_reasoning_provider import LocalReasoningProvider
from ai.providers.premium_fallback_provider import PremiumFallbackProvider
from ai.providers.rules_provider import RulesProvider
from ai.schemas import ProviderResult


class AIRouter:
    """
    Local-first AI provider router.

    Runs rules + FinBERT on every headline (cheap / free).
    Escalates to local LLM only when local confidence is insufficient.
    Escalates to premium fallback only when necessity criteria are met.
    """

    def __init__(self, config: dict[str, Any] | None = None):
        cfg = config or {}
        providers_cfg = cfg.get("providers", {})
        escalation_cfg = cfg.get("escalation", {})

        self._rules = RulesProvider(providers_cfg.get("rules", {}))
        self._fin_sentiment = FinSentimentProvider(providers_cfg.get("fin_sentiment", {}))
        self._local_llm = LocalReasoningProvider(providers_cfg.get("local_reasoning", {}))
        self._premium = PremiumFallbackProvider(providers_cfg.get("premium_fallback", {}))

        self._providers_enabled: dict[str, bool] = {
            "rules": bool(providers_cfg.get("rules", {}).get("enabled", True)),
            "fin_sentiment": bool(providers_cfg.get("fin_sentiment", {}).get("enabled", True)),
            "local_reasoning": bool(providers_cfg.get("local_reasoning", {}).get("enabled", True)),
            "premium_fallback": bool(providers_cfg.get("premium_fallback", {}).get("enabled", False)),
        }

        self._escalation_threshold = float(escalation_cfg.get("escalation_threshold", 0.55))
        self._min_confidence_local = float(escalation_cfg.get("min_confidence_for_local_acceptance", 0.55))
        self._escalation_weights = escalation_cfg.get("weights", {})
        self._emergency_keywords = (
            escalation_cfg.get("emergency_override", {}).get("always_escalate_keywords", [])
        )
        self._suppress_if_agree = bool(
            escalation_cfg.get("suppression", {}).get("suppress_if_local_providers_agree", True)
        )

        self._max_local_concurrency = 3
        self._startup_validated = False

        self._stats: dict[str, int] = {
            "rules_calls": 0, "finbert_calls": 0,
            "local_llm_calls": 0, "premium_calls": 0,
            "total_scored": 0,
        }

    async def validate_startup(self) -> bool:
        """Run startup checks on all enabled providers."""
        if self._startup_validated:
            return True

        results: dict[str, bool] = {}
        if self._providers_enabled["rules"]:
            results["rules"] = await self._rules.startup_check()
        if self._providers_enabled["fin_sentiment"]:
            results["fin_sentiment"] = await self._fin_sentiment.startup_check()
        if self._providers_enabled["local_reasoning"]:
            results["local_reasoning"] = await self._local_llm.startup_check()
        if self._providers_enabled["premium_fallback"]:
            results["premium_fallback"] = await self._premium.startup_check()

        for name, ok in results.items():
            if not ok:
                self._providers_enabled[name] = False

        active = [n for n, ok in self._providers_enabled.items() if ok]
        logger.info("ai_router | startup complete | active_providers={}", active)
        self._startup_validated = True
        return bool(active)

    async def score_batch(self, items: list[NewsItem]) -> list[Optional[NewsScore]]:
        """
        Score a batch of news items through the provider chain.

        1. Run rules on ALL items (fast, free)
        2. Run FinBERT on ALL items as a batch (fast, free after model load)
        3. Merge rules + FinBERT
        4. Identify items needing local LLM escalation
        5. Run local LLM on those (with concurrency limit)
        6. Identify items needing premium escalation
        7. Run premium on those (if enabled and criteria met)
        8. Return final NewsScore list
        """
        if not items:
            return []

        n = len(items)
        item_dicts = [
            {"headline": it.headline, "body": it.body, "source": it.source, "published_at": it.published_at}
            for it in items
        ]

        # ── Phase 1: Rules (always) ────────────────────────────────────────
        rules_results: list[ProviderResult | None] = [None] * n
        if self._providers_enabled.get("rules"):
            rules_results = await self._rules.score_batch(item_dicts)
            self._stats["rules_calls"] += n

        # ── Phase 2: FinBERT batch (always) ─────────────────────────────────
        finbert_results: list[ProviderResult | None] = [None] * n
        if self._providers_enabled.get("fin_sentiment"):
            finbert_results = await self._fin_sentiment.score_batch(item_dicts)
            self._stats["finbert_calls"] += n

        # ── Phase 3: Merge + decide escalation ─────────────────────────────
        merged: list[NewsScore | None] = [None] * n
        needs_local_llm: list[int] = []

        for i in range(n):
            rules_r = rules_results[i]
            finbert_r = finbert_results[i]

            if rules_r is not None and rules_r.is_duplicate:
                merged[i] = None
                continue

            score = self._merge_rules_and_sentiment(items[i], rules_r, finbert_r)
            merged[i] = score

            ctx = self._build_escalation_context(items[i], rules_r, finbert_r, score)
            if should_escalate_to_local_llm(ctx, min_confidence=self._min_confidence_local):
                needs_local_llm.append(i)

        # ── Phase 4: Local LLM (selective) ──────────────────────────────────
        if needs_local_llm and self._providers_enabled.get("local_reasoning"):
            semaphore = asyncio.Semaphore(self._max_local_concurrency)

            async def _run_local(idx: int) -> tuple[int, ProviderResult]:
                async with semaphore:
                    d = item_dicts[idx]
                    return idx, await self._local_llm.score_headline(
                        d["headline"], d.get("body"), d["source"], d["published_at"],
                    )

            tasks = [asyncio.create_task(_run_local(i)) for i in needs_local_llm]
            llm_results = await asyncio.gather(*tasks, return_exceptions=True)
            self._stats["local_llm_calls"] += len(needs_local_llm)

            needs_premium: list[int] = []
            for result in llm_results:
                if isinstance(result, Exception):
                    continue
                idx, llm_r = result
                if llm_r.success and (llm_r.confidence or 0) > (merged[idx].confidence if merged[idx] else 0):
                    merged[idx] = self._provider_result_to_score(items[idx], llm_r, merged[idx])
                else:
                    ctx = self._build_escalation_context(
                        items[idx], rules_results[idx], finbert_results[idx],
                        merged[idx], llm_r,
                    )
                    if should_escalate_to_premium(
                        ctx,
                        escalation_threshold=self._escalation_threshold,
                        weights=self._escalation_weights or None,
                        emergency_keywords=self._emergency_keywords,
                        fallback_enabled=self._providers_enabled.get("premium_fallback", False),
                    ):
                        needs_premium.append(idx)
        else:
            needs_premium = []
            if needs_local_llm and not self._providers_enabled.get("local_reasoning"):
                for idx in needs_local_llm:
                    ctx = self._build_escalation_context(
                        items[idx], rules_results[idx], finbert_results[idx], merged[idx],
                    )
                    if should_escalate_to_premium(
                        ctx,
                        escalation_threshold=self._escalation_threshold,
                        weights=self._escalation_weights or None,
                        emergency_keywords=self._emergency_keywords,
                        fallback_enabled=self._providers_enabled.get("premium_fallback", False),
                    ):
                        needs_premium.append(idx)

        # ── Phase 5: Premium fallback (rare) ────────────────────────────────
        if needs_premium and self._providers_enabled.get("premium_fallback"):
            for idx in needs_premium:
                try:
                    d = item_dicts[idx]
                    premium_r = await self._premium.score_headline(
                        d["headline"], d.get("body"), d["source"], d["published_at"],
                    )
                    self._stats["premium_calls"] += 1
                    if premium_r.success:
                        merged[idx] = self._provider_result_to_score(items[idx], premium_r, merged[idx])
                except Exception as exc:  # noqa: BLE001
                    logger.warning("ai_router | premium fallback failed for item {} | {}", idx, exc)

        self._stats["total_scored"] += n
        if needs_local_llm or needs_premium:
            logger.info(
                "ai_router | batch={} rules={} finbert={} llm_escalated={} premium_escalated={}",
                n, n, n, len(needs_local_llm), len(needs_premium),
            )
        return merged

    async def generate_rationale(self, signal_context: dict[str, Any]) -> str:
        """Generate trade rationale using available providers."""
        if self._providers_enabled.get("local_reasoning"):
            result = await self._local_llm.generate_rationale(signal_context)
            if result:
                return result
        if self._providers_enabled.get("premium_fallback"):
            result = await self._premium.generate_rationale(signal_context)
            if result:
                return result
        return "AI rationale unavailable (no provider available)"

    def get_stats(self) -> dict[str, int]:
        return dict(self._stats)

    # ── Merging logic ───────────────────────────────────────────────────────

    def _merge_rules_and_sentiment(
        self,
        item: NewsItem,
        rules_r: ProviderResult | None,
        finbert_r: ProviderResult | None,
    ) -> NewsScore:
        """Combine rules (tickers, events) with FinBERT (sentiment) into a NewsScore."""
        sentiment = 0.0
        confidence = 0.1
        affected: list[str] = []
        event_type = "other"
        bias = "neutral"
        rationale = ""
        decay = 12
        provider = "rules"
        total_latency = 0
        cost = 0.0

        if rules_r is not None and rules_r.success:
            affected = list(rules_r.affected_symbols)
            event_type = rules_r.event_type or "other"
            decay = rules_r.decay_hours or 12
            rationale = rules_r.rationale or ""
            total_latency += rules_r.latency_ms
            if rules_r.sentiment is not None:
                sentiment = rules_r.sentiment
                confidence = rules_r.confidence or 0.2

        if finbert_r is not None and finbert_r.success:
            sentiment = finbert_r.sentiment or 0.0
            confidence = finbert_r.confidence or 0.5
            bias = finbert_r.directional_bias or ("bullish" if sentiment > 0.1 else "bearish" if sentiment < -0.1 else "neutral")
            provider = "rules+fin_sentiment"
            total_latency += finbert_r.latency_ms

        if rules_r is not None and not (finbert_r is not None and finbert_r.success):
            provider = "rules"

        return NewsScore(
            headline=item.headline,
            sentiment=sentiment,
            confidence=confidence,
            affected_symbols=affected,
            event_type=event_type,
            directional_bias=bias,
            rationale=rationale,
            scored_at=datetime.now(timezone.utc).isoformat(),
            decay_hours=decay,
            provider=provider,
            latency_ms=total_latency,
            cost_estimate_gbp=cost,
        )

    def _provider_result_to_score(
        self,
        item: NewsItem,
        result: ProviderResult,
        existing: NewsScore | None,
    ) -> NewsScore:
        """Convert a full ProviderResult (from LLM / premium) to a NewsScore."""
        affected = list(result.affected_symbols) if result.affected_symbols else (
            list(existing.affected_symbols) if existing else []
        )
        return NewsScore(
            headline=item.headline,
            sentiment=result.sentiment or 0.0,
            confidence=result.confidence or 0.0,
            affected_symbols=affected,
            event_type=result.event_type or (existing.event_type if existing else "other"),
            directional_bias=result.directional_bias or "neutral",
            rationale=result.rationale or (existing.rationale if existing else ""),
            scored_at=datetime.now(timezone.utc).isoformat(),
            decay_hours=result.decay_hours or (existing.decay_hours if existing else 12),
            provider=result.provider_name,
            latency_ms=result.latency_ms + (existing.latency_ms if existing else 0),
            cost_estimate_gbp=result.cost_estimate_gbp + (existing.cost_estimate_gbp if existing else 0.0),
        )

    def _build_escalation_context(
        self,
        item: NewsItem,
        rules_r: ProviderResult | None,
        finbert_r: ProviderResult | None,
        merged_score: NewsScore | None,
        llm_r: ProviderResult | None = None,
    ) -> EscalationContext:
        disagreement = compute_provider_disagreement(rules_r, finbert_r)
        merged_conf = merged_score.confidence if merged_score else 0.0

        if self._suppress_if_agree and disagreement < 0.1 and merged_conf >= self._min_confidence_local:
            merged_conf = max(merged_conf, self._min_confidence_local)

        return EscalationContext(
            rules_result=rules_r,
            sentiment_result=finbert_r,
            local_llm_result=llm_r,
            merged_confidence=merged_conf,
            materiality=(rules_r.materiality if rules_r else "medium"),
            novelty_score=(rules_r.novelty_score if rules_r else 0.0),
            provider_disagreement=disagreement,
            headline=item.headline,
            source=item.source,
        )
