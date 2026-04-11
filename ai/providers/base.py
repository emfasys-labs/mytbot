"""
ai/providers/base.py
====================
Abstract base class that every AI provider must implement.
Providers produce partial or full results; the router merges and routes.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from ai.schemas import ProviderResult


class AIProvider(ABC):
    """Base class for all AI providers (rules, FinBERT, local LLM, premium)."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique provider identifier used in logs and config."""
        ...

    @abstractmethod
    async def score_headline(
        self,
        headline: str,
        body: str | None,
        source: str,
        published_at: str,
    ) -> ProviderResult:
        """Score a single headline. Return partial or full result."""
        ...

    async def score_batch(
        self,
        items: list[dict[str, Any]],
    ) -> list[ProviderResult]:
        """Score multiple headlines. Default: sequential. Override for batching."""
        results = []
        for item in items:
            r = await self.score_headline(
                item["headline"],
                item.get("body"),
                item["source"],
                item["published_at"],
            )
            results.append(r)
        return results

    async def startup_check(self) -> bool:
        """Verify the provider is available. Called once at startup."""
        return True

    async def generate_rationale(
        self,
        signal_context: dict[str, Any],
    ) -> str | None:
        """Generate plain-English trade rationale. None = not supported."""
        return None
