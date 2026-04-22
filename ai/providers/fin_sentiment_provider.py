"""
ai/providers/fin_sentiment_provider.py
======================================
Financial sentiment via FinBERT (ProsusAI/finbert), running locally.
Falls back gracefully if transformers/torch are not installed.
Batch-capable for efficiency on GPU or CPU.
"""

from __future__ import annotations

import time
from typing import Any

from loguru import logger

from ai.providers.base import AIProvider
from ai.schemas import ProviderResult

try:
    from transformers import pipeline as hf_pipeline, AutoTokenizer  # type: ignore[import-untyped]
    _HAS_TRANSFORMERS = True
except ImportError:
    _HAS_TRANSFORMERS = False

try:  # pragma: no cover - optional dependency branch
    import torch  # type: ignore[import-untyped]
    _HAS_TORCH = True
except ImportError:  # pragma: no cover - handled gracefully at runtime
    _HAS_TORCH = False


class FinSentimentProvider(AIProvider):
    """Local FinBERT sentiment on financial headlines (positive / negative / neutral)."""

    def __init__(self, config: dict[str, Any] | None = None):
        cfg = config or {}
        self._model_name = str(cfg.get("model_name", "ProsusAI/finbert"))
        self._batch_size = max(1, int(cfg.get("batch_size", 16)))
        self._max_text_length = max(32, int(cfg.get("max_text_length", 512)))
        # Device policy:
        #   auto (default): use CUDA when available, else CPU
        #   cuda: force CUDA (falls back with warning if unavailable)
        #   cpu: force CPU
        self._device_mode = str(cfg.get("device", "auto")).strip().lower()
        self._pipeline: Any = None
        self._available = False

    @property
    def name(self) -> str:
        return "fin_sentiment"

    async def startup_check(self) -> bool:
        if not _HAS_TRANSFORMERS:
            logger.warning(
                "fin_sentiment | transformers/torch not installed — provider disabled | "
                "install: pip install transformers torch"
            )
            return False
        try:
            self._load_model()
            self._available = True
            logger.info("fin_sentiment | model loaded | {}", self._model_name)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("fin_sentiment | model load failed — disabled | {}", exc)
            return False

    async def score_headline(
        self,
        headline: str,
        body: str | None,
        source: str,
        published_at: str,
    ) -> ProviderResult:
        if not self._available:
            return ProviderResult(provider_name=self.name, success=False, error="model_not_loaded")

        t0 = time.monotonic()
        text = headline[:self._max_text_length]
        try:
            result = self._pipeline(text, truncation=True, max_length=self._max_text_length)
            sentiment, confidence = self._interpret(result)
            elapsed = int((time.monotonic() - t0) * 1000)
            return ProviderResult(
                provider_name=self.name,
                sentiment=sentiment,
                confidence=confidence,
                directional_bias=("bullish" if sentiment > 0.1 else "bearish" if sentiment < -0.1 else "neutral"),
                latency_ms=elapsed,
                success=True,
            )
        except Exception as exc:  # noqa: BLE001
            elapsed = int((time.monotonic() - t0) * 1000)
            logger.warning("fin_sentiment | scoring failed | {}", exc)
            return ProviderResult(
                provider_name=self.name, success=False,
                error=str(exc)[:200], latency_ms=elapsed,
            )

    async def score_batch(self, items: list[dict[str, Any]]) -> list[ProviderResult]:
        if not self._available:
            return [ProviderResult(provider_name=self.name, success=False, error="model_not_loaded")
                    for _ in items]

        t0 = time.monotonic()
        texts = [item["headline"][:self._max_text_length] for item in items]
        try:
            raw_results = self._pipeline(
                texts, truncation=True, max_length=self._max_text_length,
                batch_size=self._batch_size,
            )
            elapsed = int((time.monotonic() - t0) * 1000)
            per_item_ms = elapsed // max(1, len(items))

            results = []
            for raw in raw_results:
                sentiment, confidence = self._interpret(raw)
                results.append(ProviderResult(
                    provider_name=self.name,
                    sentiment=sentiment,
                    confidence=confidence,
                    directional_bias=("bullish" if sentiment > 0.1 else "bearish" if sentiment < -0.1 else "neutral"),
                    latency_ms=per_item_ms,
                    success=True,
                ))
            logger.debug("fin_sentiment | batch scored {} items in {}ms", len(items), elapsed)
            return results
        except Exception as exc:  # noqa: BLE001
            elapsed = int((time.monotonic() - t0) * 1000)
            logger.warning("fin_sentiment | batch scoring failed | {}", exc)
            return [ProviderResult(provider_name=self.name, success=False,
                                   error=str(exc)[:200], latency_ms=elapsed)
                    for _ in items]

    def _load_model(self) -> None:
        if self._pipeline is not None:
            return
        device = self._resolve_device()
        self._pipeline = hf_pipeline(
            "sentiment-analysis",
            model=self._model_name,
            tokenizer=self._model_name,
            return_all_scores=False,
            device=device,
        )
        if device >= 0:
            logger.info("fin_sentiment | device=cuda:{}", device)
        else:
            logger.info("fin_sentiment | device=cpu")

    def _resolve_device(self) -> int:
        """Transformers pipeline device index: -1=CPU, >=0 CUDA index."""
        if self._device_mode == "cpu":
            return -1
        if self._device_mode == "cuda":
            if _HAS_TORCH and bool(torch.cuda.is_available()):  # type: ignore[name-defined]
                return 0
            logger.warning("fin_sentiment | device=cuda requested but CUDA unavailable, falling back to CPU")
            return -1
        # auto
        if _HAS_TORCH and bool(torch.cuda.is_available()):  # type: ignore[name-defined]
            return 0
        return -1

    @staticmethod
    def _interpret(result: Any) -> tuple[float, float]:
        """Convert FinBERT output to (sentiment_float, confidence)."""
        if isinstance(result, list):
            result = result[0] if result else {"label": "neutral", "score": 0.5}
        label = str(result.get("label", "neutral")).lower()
        score = float(result.get("score", 0.5))
        if label == "positive":
            return round(score, 4), round(score, 4)
        if label == "negative":
            return round(-score, 4), round(score, 4)
        return 0.0, round(score, 4)
