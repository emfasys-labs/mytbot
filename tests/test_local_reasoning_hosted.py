"""
tests/test_local_reasoning_hosted.py
====================================
Covers the hosted OpenAI-compatible path added to LocalReasoningProvider so the
same provider can talk to Gemini / Groq / OpenRouter (API key + auth header +
trust-configured-model), while keeping keyless local Ollama behaviour unchanged.
"""

from __future__ import annotations

import json

import httpx
import pytest

import ai.providers.local_reasoning_provider as lrp_mod
from ai.providers.local_reasoning_provider import LocalReasoningProvider

_GOOD_JSON = {
    "sentiment": 0.8,
    "confidence": 0.9,
    "affected_symbols": ["AAPL"],
    "event_type": "earnings",
    "directional_bias": "bullish",
    "rationale": "Strong results.",
    "decay_hours": 24,
}


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("err", request=None, response=None)


class _FakeAsyncClient:
    """Records the headers/urls of every request so tests can assert on auth."""

    captured: list[dict] = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, headers=None, **kwargs):
        _FakeAsyncClient.captured.append({"method": "GET", "url": url, "headers": headers or {}})
        # OpenAI /models style listing that does NOT contain the configured model id verbatim.
        return _FakeResponse(200, {"data": [{"id": "models/some-other-model"}]})

    async def post(self, url, json=None, headers=None, **kwargs):
        _FakeAsyncClient.captured.append({"method": "POST", "url": url, "headers": headers or {}})
        content = json_dumps(_GOOD_JSON)
        if url.endswith("/api/chat"):  # Ollama response shape
            return _FakeResponse(200, {"message": {"content": content}})
        return _FakeResponse(200, {"choices": [{"message": {"content": content}}]})


def json_dumps(d: dict) -> str:
    return json.dumps(d)


@pytest.fixture(autouse=True)
def _patch_httpx(monkeypatch):
    _FakeAsyncClient.captured = []
    monkeypatch.setattr(lrp_mod.httpx, "AsyncClient", _FakeAsyncClient)
    yield


def test_auth_header_present_for_hosted_key():
    prov = LocalReasoningProvider({
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "model_name": "gemini-2.0-flash",
        "api_key": "secret-key",
    })
    assert prov._api_style == "openai"
    assert prov._auth_headers() == {"Authorization": "Bearer secret-key"}
    assert prov._trust_configured_model is True


def test_no_auth_header_for_keyless_ollama():
    prov = LocalReasoningProvider({"base_url": "http://localhost:11434", "model_name": "x"})
    assert prov._api_style == "ollama"
    assert prov._auth_headers() == {}
    assert prov._trust_configured_model is False


def test_api_key_from_env(monkeypatch):
    monkeypatch.setenv("MY_GEMINI_KEY", "env-key-123")
    prov = LocalReasoningProvider({
        "base_url": "https://x/v1",
        "model_name": "m",
        "api_key_env": "MY_GEMINI_KEY",
    })
    assert prov._api_key == "env-key-123"
    assert prov._auth_headers() == {"Authorization": "Bearer env-key-123"}


@pytest.mark.asyncio
async def test_hosted_startup_trusts_configured_model_and_sends_auth():
    prov = LocalReasoningProvider({
        "base_url": "https://api.groq.com/openai/v1",
        "model_name": "llama-3.3-70b-versatile",
        "api_key": "k",
    })
    ok = await prov.startup_check()
    # Even though /models did not list the configured model, hosted+key trusts it.
    assert ok is True
    assert prov._available is True
    assert prov._active_model == "llama-3.3-70b-versatile"
    get_calls = [c for c in _FakeAsyncClient.captured if c["method"] == "GET"]
    assert get_calls and get_calls[0]["headers"].get("Authorization") == "Bearer k"


@pytest.mark.asyncio
async def test_hosted_score_headline_posts_with_auth_and_parses_json():
    prov = LocalReasoningProvider({
        "base_url": "https://x/v1",
        "model_name": "m",
        "api_key": "abc",
    })
    assert await prov.startup_check() is True
    r = await prov.score_headline("Apple beats earnings", None, "Reuters", "2026-01-01T00:00:00Z")
    assert r.success is True
    assert r.directional_bias == "bullish"
    assert r.sentiment == pytest.approx(0.8)
    assert "AAPL" in r.affected_symbols
    post_calls = [c for c in _FakeAsyncClient.captured if c["method"] == "POST"]
    assert post_calls and post_calls[0]["url"].endswith("/chat/completions")
    assert post_calls[0]["headers"].get("Authorization") == "Bearer abc"


@pytest.mark.asyncio
async def test_keyless_ollama_post_has_no_auth_header():
    prov = LocalReasoningProvider({"base_url": "http://localhost:11434", "model_name": "m"})
    # Force availability without relying on the model-listing match.
    prov._available = True
    prov._active_model = "m"
    r = await prov.score_headline("h", None, "s", "2026-01-01T00:00:00Z")
    assert r.success is True
    post_calls = [c for c in _FakeAsyncClient.captured if c["method"] == "POST"]
    assert post_calls and post_calls[0]["url"].endswith("/api/chat")
    assert "Authorization" not in post_calls[0]["headers"]
