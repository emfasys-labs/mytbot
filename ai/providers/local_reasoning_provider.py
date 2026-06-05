"""
ai/providers/local_reasoning_provider.py
========================================
Local LLM via Ollama (or any OpenAI-compatible endpoint).
Full headline classification with JSON output.
Used when rules + FinBERT are insufficient.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import httpx
from loguru import logger

from ai.providers.base import AIProvider
from ai.schemas import ProviderResult

_SYSTEM_PROMPT = """You are a financial news analyst for an autonomous trading system.
Analyse the headline and return structured JSON scoring its market impact.

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


class LocalReasoningProvider(AIProvider):
    """Local LLM (Ollama or OpenAI-compatible) for full news classification.

    Supports a primary model with an optional fallback model.
    If the primary model fails or is unavailable, the fallback is tried automatically.
    """

    def __init__(self, config: dict[str, Any] | None = None):
        cfg = config or {}
        self._base_url = str(cfg.get("base_url", "http://localhost:11434")).rstrip("/")
        self._model = str(cfg.get("model_name", "qwen2.5:7b"))
        self._fallback_model: str | None = cfg.get("fallback_model") or None
        self._temperature = float(cfg.get("temperature", 0.1))
        self._max_tokens = int(cfg.get("max_tokens", 400))
        self._timeout = float(cfg.get("timeout_seconds", 15))
        self._failure_cooldown_seconds = float(cfg.get("failure_cooldown_seconds", self._timeout))
        self._disabled_until: float = 0.0
        self._use_json_mode = bool(cfg.get("use_json_mode", True))
        self._available = False
        self._active_model: str | None = None

        is_openai_compat = "/v1" in self._base_url
        if is_openai_compat:
            self._api_style = "openai"
        else:
            self._api_style = "ollama"

    @property
    def name(self) -> str:
        return "local_reasoning"

    async def startup_check(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                if self._api_style == "ollama":
                    resp = await client.get(f"{self._base_url}/api/tags")
                else:
                    resp = await client.get(f"{self._base_url}/models")
                if resp.status_code != 200:
                    logger.warning("local_reasoning | endpoint returned {} — disabled", resp.status_code)
                    return False

            installed = self._parse_installed_models(resp)
            candidates = [self._model]
            if self._fallback_model:
                candidates.append(self._fallback_model)

            for model in candidates:
                if self._model_available(model, installed):
                    self._active_model = model
                    self._available = True
                    self._disabled_until = 0.0
                    label = "primary" if model == self._model else "fallback"
                    logger.info(
                        "local_reasoning | {} model active | model={} url={} style={}",
                        label, model, self._base_url, self._api_style,
                    )
                    if model != self._model:
                        logger.warning(
                            "local_reasoning | primary model '{}' not found, using fallback '{}'",
                            self._model, model,
                        )
                    return True

            logger.warning(
                "local_reasoning | none of {} found at {} — disabled",
                candidates, self._base_url,
            )
            return False
        except Exception as exc:  # noqa: BLE001
            logger.warning("local_reasoning | endpoint unreachable — disabled | {}", exc)
            return False

    def _parse_installed_models(self, resp: httpx.Response) -> set[str]:
        """Extract installed model names from Ollama /api/tags or OpenAI /models."""
        try:
            data = resp.json()
            if self._api_style == "ollama":
                return {m.get("name", "") for m in data.get("models", [])}
            else:
                return {m.get("id", "") for m in data.get("data", [])}
        except Exception:  # noqa: BLE001
            return set()

    @staticmethod
    def _model_available(target: str, installed: set[str]) -> bool:
        """Check if target model is in installed set (handles tag variants)."""
        if target in installed:
            return True
        if ":" not in target and f"{target}:latest" in installed:
            return True
        for name in installed:
            if name.split(":")[0] == target.split(":")[0]:
                return True
        return False

    async def score_headline(
        self,
        headline: str,
        body: str | None,
        source: str,
        published_at: str,
    ) -> ProviderResult:
        if not self._available:
            return ProviderResult(provider_name=self.name, success=False, error="endpoint_unavailable")
        if self._cooldown_active():
            return ProviderResult(provider_name=self.name, success=False, error="cooldown_after_timeout")

        t0 = time.monotonic()
        body_preview = (body or "").strip()[:800]
        user_msg = (
            f"Headline: {headline}\n"
            f"Source: {source}\n"
            f"PublishedAt: {published_at}\n"
            f"BodyPreview: {body_preview}\n"
            "Return JSON only."
        )

        models_to_try = [self._active_model or self._model]
        if self._fallback_model and self._fallback_model != models_to_try[0]:
            models_to_try.append(self._fallback_model)

        last_error = ""
        for model in models_to_try:
            try:
                raw_text = await self._call_llm(user_msg, model_override=model)
                data = self._extract_json(raw_text)
                elapsed = int((time.monotonic() - t0) * 1000)
                result = self._parse_into_result(data, elapsed)
                result.provider_name = f"local_reasoning({model})"
                return result
            except Exception as exc:  # noqa: BLE001
                last_error = str(exc)[:200]
                if model != models_to_try[-1]:
                    logger.warning(
                        "local_reasoning | model '{}' failed, trying fallback | {}",
                        model, last_error,
                    )
                if isinstance(exc, (TimeoutError, httpx.TimeoutException, asyncio.TimeoutError)):
                    self._trip_cooldown(model=model, error=last_error)
                    break

        elapsed = int((time.monotonic() - t0) * 1000)
        logger.warning("local_reasoning | all models failed ({}ms) | {}", elapsed, last_error)
        return ProviderResult(
            provider_name=self.name, success=False,
            error=last_error, latency_ms=elapsed,
        )

    async def score_headline_ensemble(
        self,
        headline: str,
        body: str | None,
        source: str,
        published_at: str,
    ) -> tuple[ProviderResult, ProviderResult | None]:
        """Run both primary and fallback models in parallel, return both results.

        Returns (primary_result, secondary_result). secondary is None if
        no fallback model is configured or only one model is installed.
        """
        if not self._available:
            return (
                ProviderResult(provider_name=self.name, success=False, error="endpoint_unavailable"),
                None,
            )
        if self._cooldown_active():
            return (
                ProviderResult(provider_name=self.name, success=False, error="cooldown_after_timeout"),
                None,
            )

        body_preview = (body or "").strip()[:800]
        user_msg = (
            f"Headline: {headline}\n"
            f"Source: {source}\n"
            f"PublishedAt: {published_at}\n"
            f"BodyPreview: {body_preview}\n"
            "Return JSON only."
        )

        primary = self._active_model or self._model
        secondary = self._fallback_model if (self._fallback_model and self._fallback_model != primary) else None

        async def _run_model(model: str) -> ProviderResult:
            t0 = time.monotonic()
            try:
                raw = await self._call_llm(user_msg, model_override=model)
                data = self._extract_json(raw)
                elapsed = int((time.monotonic() - t0) * 1000)
                result = self._parse_into_result(data, elapsed)
                result.provider_name = f"local_reasoning({model})"
                return result
            except Exception as exc:  # noqa: BLE001
                elapsed = int((time.monotonic() - t0) * 1000)
                if isinstance(exc, (TimeoutError, httpx.TimeoutException, asyncio.TimeoutError)):
                    self._trip_cooldown(model=model, error=str(exc)[:200])
                return ProviderResult(
                    provider_name=f"local_reasoning({model})",
                    success=False, error=str(exc)[:200], latency_ms=elapsed,
                )

        if secondary:
            primary_r, secondary_r = await asyncio.gather(
                _run_model(primary), _run_model(secondary),
            )
            return primary_r, secondary_r

        return await _run_model(primary), None

    async def generate_rationale(self, signal_context: dict[str, Any]) -> str | None:
        if not self._available:
            return None
        if self._cooldown_active():
            return None
        prompt = (
            f"Given this trade context, write one clear sentence explaining "
            f"why this trade makes sense:\n\n"
            f"Symbol: {signal_context.get('symbol')}\n"
            f"Side: {signal_context.get('side')}\n"
            f"Strategy: {signal_context.get('strategy')}\n"
            f"Confidence: {signal_context.get('confidence')}\n"
            f"News score: {signal_context.get('news_score')}\n\n"
            f"Respond with ONE sentence only. Plain English."
        )
        try:
            raw = await self._call_llm(prompt, system="You are a trading auditor. Return one sentence.")
            one_line = " ".join(raw.strip().split())
            return one_line[:280] if one_line else None
        except Exception as exc:  # noqa: BLE001
            logger.warning("local_reasoning | rationale failed | {}", exc)
            return None

    def _cooldown_active(self) -> bool:
        return time.monotonic() < self._disabled_until

    def _trip_cooldown(self, *, model: str, error: str) -> None:
        cooldown = max(0.0, self._failure_cooldown_seconds)
        if cooldown <= 0:
            return
        self._disabled_until = max(self._disabled_until, time.monotonic() + cooldown)
        logger.warning(
            "local_reasoning | model timeout; cooling down local LLM | model={} cooldown_sec={} | {}",
            model,
            cooldown,
            error,
        )

    async def _call_llm(
        self, user_msg: str, system: str | None = None, model_override: str | None = None,
    ) -> str:
        system_prompt = system or _SYSTEM_PROMPT
        model = model_override or self._active_model or self._model
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            if self._api_style == "ollama":
                payload: dict[str, Any] = {
                    "model": model,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_msg},
                    ],
                    "stream": False,
                    "options": {
                        "temperature": self._temperature,
                        "num_predict": self._max_tokens,
                    },
                }
                if self._use_json_mode:
                    payload["format"] = "json"
                resp = await client.post(f"{self._base_url}/api/chat", json=payload)
                resp.raise_for_status()
                data = resp.json()
                return str(data.get("message", {}).get("content", ""))
            else:
                payload = {
                    "model": model,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_msg},
                    ],
                    "temperature": self._temperature,
                    "max_tokens": self._max_tokens,
                }
                if self._use_json_mode:
                    payload["response_format"] = {"type": "json_object"}
                resp = await client.post(f"{self._base_url}/chat/completions", json=payload)
                resp.raise_for_status()
                data = resp.json()
                return str(data["choices"][0]["message"]["content"])

    def _parse_into_result(self, data: dict[str, Any], elapsed_ms: int) -> ProviderResult:
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
            cost_estimate_gbp=0.0,
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
        raise ValueError("No JSON in local LLM response")

    @staticmethod
    def _clamp(v: Any, lo: float, hi: float, default: float) -> float:
        try:
            return max(lo, min(hi, float(v)))
        except Exception:  # noqa: BLE001
            return default
