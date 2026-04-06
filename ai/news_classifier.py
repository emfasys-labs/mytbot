"""
AI News Intelligence Layer (M6).

Uses Claude API to classify headline impact and generate rationale.
The AI NEVER places orders. It only scores and explains.
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from anthropic import AsyncAnthropic
from loguru import logger


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
        self.model = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-5")
        self.max_retries = int(os.getenv("AI_NEWS_MAX_RETRIES", "2"))
        self.timeout_sec = float(os.getenv("AI_NEWS_TIMEOUT_SEC", "20"))
        self._client: AsyncAnthropic | None = None

    def _client_or_init(self) -> AsyncAnthropic:
        if self._client is None:
            self._client = AsyncAnthropic(api_key=self.api_key, timeout=self.timeout_sec)
        return self._client

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
        except Exception as e:  # noqa: BLE001
            logger.warning("news_classifier | score failed | {}", e)
            return None

    async def score_batch(self, items: list[NewsItem]) -> list[Optional[NewsScore]]:
        """Score multiple news items."""
        if not items:
            return []
        out: list[Optional[NewsScore]] = []
        for item in items:
            out.append(await self.score(item))
        return out

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
            and self._parse_iso(s.scored_at) > cutoff
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
            raw = await self._call_text(
                system_prompt=(
                    "You are an execution-side auditor for a trading system. "
                    "Return exactly one plain-English sentence. "
                    "No markdown, no bullet points, no prefacing."
                ),
                user_prompt=prompt,
                max_tokens=120,
            )
            one_line = " ".join(raw.strip().split())
            return one_line[:280] if one_line else "Rationale unavailable"
        except Exception as e:  # noqa: BLE001
            logger.warning("news_classifier | rationale generation failed | {}", e)
            return "Rationale generation failed"

    async def _call_claude(self, news_item: NewsItem) -> str:
        """Call Claude API for news classification."""
        body_preview = (news_item.body or "").strip()[:1200]
        user = (
            f"Headline: {news_item.headline}\n"
            f"Source: {news_item.source}\n"
            f"PublishedAt: {news_item.published_at}\n"
            f"BodyPreview: {body_preview}\n"
            "Return JSON only."
        )
        return await self._call_text(self.SYSTEM_PROMPT, user, max_tokens=500)

    def _parse_response(self, news_item: NewsItem, raw: str) -> NewsScore:
        """Parse Claude's JSON response into a NewsScore."""
        data = self._extract_json(raw)
        sentiment = self._bounded_float(data.get("sentiment"), lo=-1.0, hi=1.0, default=0.0)
        confidence = self._bounded_float(data.get("confidence"), lo=0.0, hi=1.0, default=0.0)
        event_type = self._safe_event_type(str(data.get("event_type", "other")))
        directional_bias = self._safe_bias(str(data.get("directional_bias", "neutral")))
        rationale = str(data.get("rationale", "")).strip() or "No rationale returned."
        decay_hours = self._bounded_int(data.get("decay_hours"), lo=1, hi=168, default=24)

        affected_raw = data.get("affected_symbols", [])
        affected: list[str] = []
        if isinstance(affected_raw, list):
            for x in affected_raw[:20]:
                s = str(x).strip().upper()
                if s:
                    affected.append(s)
        affected = sorted(set(affected))

        return NewsScore(
            headline=news_item.headline,
            sentiment=sentiment,
            confidence=confidence,
            affected_symbols=affected,
            event_type=event_type,
            directional_bias=directional_bias,
            rationale=rationale,
            scored_at=datetime.now(timezone.utc).isoformat(),
            decay_hours=decay_hours,
        )

    async def _call_text(self, system_prompt: str, user_prompt: str, *, max_tokens: int) -> str:
        client = self._client_or_init()
        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                resp = await client.messages.create(
                    model=self.model,
                    max_tokens=max_tokens,
                    temperature=0.0,
                    system=system_prompt,
                    messages=[{"role": "user", "content": user_prompt}],
                )
                text_chunks: list[str] = []
                for block in resp.content:
                    t = getattr(block, "text", None)
                    if t:
                        text_chunks.append(str(t))
                joined = "\n".join(text_chunks).strip()
                if joined:
                    return joined
                raise ValueError("Claude returned empty response")
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                if attempt >= self.max_retries:
                    break
                await asyncio.sleep(0.5 * (2**attempt))
        raise RuntimeError(f"Claude API call failed after retries: {last_exc}")

    @staticmethod
    def _extract_json(raw: str) -> dict[str, Any]:
        text = raw.strip()
        try:
            return json.loads(text)
        except Exception:  # noqa: BLE001
            pass
        # recover fenced or wrapped output
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start : end + 1])
        raise ValueError("No JSON object found in Claude response")

    @staticmethod
    def _bounded_float(v: Any, *, lo: float, hi: float, default: float) -> float:
        try:
            x = float(v)
        except Exception:  # noqa: BLE001
            return default
        return max(lo, min(hi, x))

    @staticmethod
    def _bounded_int(v: Any, *, lo: int, hi: int, default: int) -> int:
        try:
            x = int(v)
        except Exception:  # noqa: BLE001
            return default
        return max(lo, min(hi, x))

    @staticmethod
    def _safe_event_type(v: str) -> str:
        allowed = {
            "earnings",
            "macro",
            "regulatory",
            "geopolitical",
            "sector",
            "company",
            "crypto",
            "other",
        }
        val = v.strip().lower()
        return val if val in allowed else "other"

    @staticmethod
    def _safe_bias(v: str) -> str:
        allowed = {"bullish", "bearish", "neutral"}
        val = v.strip().lower()
        return val if val in allowed else "neutral"

    @staticmethod
    def _parse_iso(v: str) -> datetime:
        txt = v.strip()
        if txt.endswith("Z"):
            txt = txt[:-1] + "+00:00"
        dt = datetime.fromisoformat(txt)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
