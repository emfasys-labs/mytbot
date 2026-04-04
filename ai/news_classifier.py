"""
ai/news_classifier.py
======================
AI News Intelligence Layer (M6).

Uses Claude API to:
1. Classify news events by type and affected assets
2. Score sentiment and directional bias (-1.0 to +1.0)
3. Assign confidence and time-decay (news fades)
4. Generate plain-English trade rationale

The AI NEVER places orders. It only scores and explains.
The risk engine and strategy engine make the actual decisions.
"""

import os
import json
import logging
from dataclasses import dataclass
from typing import Optional
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)


@dataclass
class NewsItem:
    headline: str
    source: str
    published_at: str
    body: Optional[str] = None


@dataclass
class NewsScore:
    headline: str
    sentiment: float            # -1.0 (very negative) → +1.0 (very positive)
    confidence: float           # 0.0 → 1.0
    affected_symbols: list[str]
    event_type: str             # "earnings", "macro", "regulatory", "geopolitical", etc.
    directional_bias: str       # "bullish", "bearish", "neutral"
    rationale: str              # plain English explanation
    scored_at: str
    decay_hours: int            # how long before this signal loses relevance


class NewsClassifier:
    """
    Classifies financial news using Claude API.
    Returns structured scores for use by the signal engine.
    """

    SYSTEM_PROMPT = """You are a financial news analyst for an autonomous trading system.
Your job is to analyse news headlines and return structured JSON scoring their market impact.

Always respond with valid JSON only. No preamble, no explanation outside the JSON.

JSON schema:
{
  "sentiment": <float -1.0 to 1.0>,
  "confidence": <float 0.0 to 1.0>,
  "affected_symbols": [<list of ticker symbols>],
  "event_type": <"earnings"|"macro"|"regulatory"|"geopolitical"|"sector"|"company"|"crypto"|"other">,
  "directional_bias": <"bullish"|"bearish"|"neutral">,
  "rationale": <one sentence plain English explanation>,
  "decay_hours": <integer: how many hours before this news loses market relevance>
}

Rules:
- sentiment: -1.0 = catastrophic news, 0.0 = neutral, +1.0 = extremely positive
- confidence: how certain you are about the directional impact
- affected_symbols: use standard ticker symbols (e.g. AAPL, BTC, SPY, GBP)
- decay_hours: breaking news = 2-6hrs, earnings = 24hrs, macro = 48-72hrs
- rationale: one clear sentence, no jargon
"""

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.getenv("ANTHROPIC_API_KEY", "")
        self._client = None

    async def score(self, news_item: NewsItem) -> Optional[NewsScore]:
        """
        Score a single news item.
        Returns NewsScore or None if classification fails.
        """
        if not self.api_key:
            logger.warning("No ANTHROPIC_API_KEY set — news classification disabled")
            return None

        try:
            response = await self._call_claude(news_item)
            return self._parse_response(news_item, response)
        except Exception as e:
            logger.error(f"News classification failed: {e}")
            return None

    async def score_batch(self, items: list[NewsItem]) -> list[Optional[NewsScore]]:
        """Score multiple news items."""
        results = []
        for item in items:
            score = await self.score(item)
            results.append(score)
        return results

    def get_symbol_score(
        self,
        symbol: str,
        scores: list[NewsScore],
        max_age_hours: int = 4,
    ) -> Optional[float]:
        """
        Get the aggregate news score for a specific symbol.
        Only considers scores within max_age_hours.
        Returns float -1.0 to 1.0, or None if no relevant news.
        """
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(hours=max_age_hours)

        relevant = [
            s for s in scores
            if symbol in s.affected_symbols
            and datetime.fromisoformat(s.scored_at) > cutoff
        ]

        if not relevant:
            return None

        # Weighted average by confidence
        total_weight = sum(s.confidence for s in relevant)
        if total_weight == 0:
            return None

        weighted_sentiment = sum(
            s.sentiment * s.confidence for s in relevant
        ) / total_weight

        return weighted_sentiment

    async def generate_rationale(self, signal_context: dict) -> str:
        """
        Generate a plain-English explanation of why a trade is being made.
        Used for the audit log and dashboard.
        """
        if not self.api_key:
            return "AI rationale unavailable (no API key configured)"

        prompt = f"""Given this trade context, write one clear sentence explaining why this trade makes sense:

Symbol: {signal_context.get('symbol')}
Side: {signal_context.get('side')}
Strategy: {signal_context.get('strategy')}
Confidence: {signal_context.get('confidence')}
News score: {signal_context.get('news_score')}
Key features: {signal_context.get('metadata', {})}

Respond with ONE sentence only. Plain English. No jargon."""

        try:
            # TODO M6: implement actual API call
            return f"Momentum breakout detected on {signal_context.get('symbol')} with confirming volume."
        except Exception as e:
            logger.error(f"Rationale generation failed: {e}")
            return "Rationale generation failed"

    async def _call_claude(self, news_item: NewsItem) -> str:
        """Call Claude API for news classification."""
        # TODO M6: implement using anthropic SDK
        # import anthropic
        # client = anthropic.AsyncAnthropic(api_key=self.api_key)
        # message = await client.messages.create(
        #     model="claude-sonnet-4-6",
        #     max_tokens=500,
        #     system=self.SYSTEM_PROMPT,
        #     messages=[{"role": "user", "content": f"Headline: {news_item.headline}"}]
        # )
        # return message.content[0].text
        raise NotImplementedError("Implement in M6")

    def _parse_response(self, news_item: NewsItem, raw: str) -> NewsScore:
        """Parse Claude's JSON response into a NewsScore."""
        data = json.loads(raw)
        return NewsScore(
            headline=news_item.headline,
            sentiment=float(data["sentiment"]),
            confidence=float(data["confidence"]),
            affected_symbols=data["affected_symbols"],
            event_type=data["event_type"],
            directional_bias=data["directional_bias"],
            rationale=data["rationale"],
            scored_at=datetime.now(timezone.utc).isoformat(),
            decay_hours=int(data["decay_hours"]),
        )
