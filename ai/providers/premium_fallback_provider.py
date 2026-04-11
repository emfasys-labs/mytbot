"""
ai/providers/premium_fallback_provider.py
=========================================
Premium paid LLM (Anthropic Claude) — used ONLY when local providers
cannot resolve an important ambiguity.  Disabled by default.

This is an escalation path, not the default.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any

from loguru import logger

from ai.providers.base import AIProvider
from ai.schemas import ProviderResult

_SYSTEM_PROMPT = """You are a financial news analyst for an autonomous trading system.
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

_GBP_PER_INPUT_TOKEN = 0.000002
_GBP_PER_OUTPUT_TOKEN = 0.000008


class PremiumFallbackProvider(AIProvider):
    """Anthropic Claude — expensive, high-quality, only for escalated events."""

    def __init__(self, config: dict[str, Any] | None = None):
        cfg = config or {}
        self._api_key = str(cfg.get("api_key", "") or os.getenv("ANTHROPIC_API_KEY", ""))
        self._model = str(cfg.get("model_name", "claude-sonnet-4-5"))
        self._timeout = float(cfg.get("timeout_seconds", 20))
        self._max_tokens = int(cfg.get("max_tokens", 500))
        self._max_retries = 2
        self._client: Any = None
        self._available = False

    @property
    def name(self) -> str:
        return "premium_fallback"

    async def startup_check(self) -> bool:
        if not self._api_key:
            logger.info("premium_fallback | no ANTHROPIC_API_KEY — provider disabled (expected for local-first)")
            return False
        try:
            from anthropic import AsyncAnthropic
            self._client = AsyncAnthropic(api_key=self._api_key, timeout=self._timeout)
            raw = await self._call_text(
                system_prompt='Return exactly this JSON: {"ok":true}',
                user_prompt="healthcheck",
                max_tokens=16,
            )
            data = self._extract_json(raw)
            ok = bool(data.get("ok", False))
            if ok:
                self._available = True
                logger.info("premium_fallback | validated | model={}", self._model)
                return True
            logger.warning("premium_fallback | unexpected healthcheck response")
            return False
        except Exception as exc:  # noqa: BLE001
            logger.warning("premium_fallback | startup check failed | {}", exc)
            return False

    async def score_headline(
        self,
        headline: str,
        body: str | None,
        source: str,
        published_at: str,
    ) -> ProviderResult:
        if not self._available:
            return ProviderResult(provider_name=self.name, success=False, error="not_available")

        t0 = time.monotonic()
        body_preview = (body or "").strip()[:1200]
        user_msg = (
            f"Headline: {headline}\n"
            f"Source: {source}\n"
            f"PublishedAt: {published_at}\n"
            f"BodyPreview: {body_preview}\n"
            "Return JSON only."
        )

        try:
            raw = await self._call_text(_SYSTEM_PROMPT, user_msg, max_tokens=self._max_tokens)
            data = self._extract_json(raw)
            elapsed = int((time.monotonic() - t0) * 1000)
            est_tokens = len(user_msg.split()) * 2 + len(raw.split()) * 2
            cost = est_tokens * (_GBP_PER_INPUT_TOKEN + _GBP_PER_OUTPUT_TOKEN) / 2
            return self._parse_into_result(data, elapsed, cost)
        except Exception as exc:  # noqa: BLE001
            elapsed = int((time.monotonic() - t0) * 1000)
            logger.warning("premium_fallback | scoring failed ({}ms) | {}", elapsed, exc)
            return ProviderResult(
                provider_name=self.name, success=False,
                error=str(exc)[:200], latency_ms=elapsed,
            )

    async def generate_rationale(self, signal_context: dict[str, Any]) -> str | None:
        if not self._available:
            return None
        prompt = (
            f"Given this trade context, write one clear sentence explaining "
            f"why this trade makes sense:\n\n"
            f"Symbol: {signal_context.get('symbol')}\n"
            f"Side: {signal_context.get('side')}\n"
            f"Strategy: {signal_context.get('strategy')}\n"
            f"Confidence: {signal_context.get('confidence')}\n"
            f"News score: {signal_context.get('news_score')}\n\n"
            f"Respond with ONE sentence only. Plain English. No jargon."
        )
        try:
            raw = await self._call_text(
                system_prompt=(
                    "You are an execution-side auditor for a trading system. "
                    "Return exactly one plain-English sentence."
                ),
                user_prompt=prompt,
                max_tokens=120,
            )
            one_line = " ".join(raw.strip().split())
            return one_line[:280] if one_line else None
        except Exception as exc:  # noqa: BLE001
            logger.warning("premium_fallback | rationale failed | {}", exc)
            return None

    async def _call_text(self, system_prompt: str, user_prompt: str, *, max_tokens: int) -> str:
        last_exc: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                resp = await self._client.messages.create(
                    model=self._model,
                    max_tokens=max_tokens,
                    temperature=0.0,
                    system=system_prompt,
                    messages=[{"role": "user", "content": user_prompt}],
                )
                chunks: list[str] = []
                for block in resp.content:
                    t = getattr(block, "text", None)
                    if t:
                        chunks.append(str(t))
                joined = "\n".join(chunks).strip()
                if joined:
                    return joined
                raise ValueError("Claude returned empty response")
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                if attempt >= self._max_retries:
                    break
                await asyncio.sleep(0.5 * (2 ** attempt))
        raise RuntimeError(f"Claude API failed after retries: {last_exc}")

    def _parse_into_result(self, data: dict[str, Any], elapsed_ms: int, cost: float) -> ProviderResult:
        sentiment = self._clamp(data.get("sentiment"), -1.0, 1.0, 0.0)
        confidence = self._clamp(data.get("confidence"), 0.0, 1.0, 0.0)
        event_type = str(data.get("event_type", "other")).strip().lower()
        bias = str(data.get("directional_bias", "neutral")).strip().lower()
        rationale = str(data.get("rationale", ""))[:500]
        decay = max(1, min(168, int(data.get("decay_hours", 24))))
        affected_raw = data.get("affected_symbols", [])
        affected = sorted({str(x).strip().upper() for x in (affected_raw if isinstance(affected_raw, list) else []) if str(x).strip()})

        return ProviderResult(
            provider_name=self.name,
            sentiment=sentiment,
            confidence=confidence,
            directional_bias=bias if bias in ("bullish", "bearish", "neutral") else "neutral",
            affected_symbols=affected,
            event_type=event_type,
            decay_hours=decay,
            rationale=rationale,
            latency_ms=elapsed_ms,
            cost_estimate_gbp=round(cost, 6),
            success=True,
        )

    @staticmethod
    def _extract_json(raw: str) -> dict[str, Any]:
        text = raw.strip()
        try:
            return json.loads(text)
        except Exception:  # noqa: BLE001
            pass
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start : end + 1])
        raise ValueError("No JSON in Claude response")

    @staticmethod
    def _clamp(v: Any, lo: float, hi: float, default: float) -> float:
        try:
            return max(lo, min(hi, float(v)))
        except Exception:  # noqa: BLE001
            return default
