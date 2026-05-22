"""
connectors/premium_llm.py
==========================
D127 Connect Hub v2 — Phase 5: Premium LLM provider picker + cert.

The Premium LLM is the optional paid escalation/arbiter stage. The
operator picks a provider from the supported catalogue
(`config/premium_llm_catalogue.yaml`), supplies an API key (and, for
Azure / custom, an endpoint), and runs a compatibility test before the
provider is activated.

The premium LLM only ADVISES — it never executes — so a custom
OpenAI-compatible endpoint is acceptable. It still must pass the test.

Two endpoint shapes cover every provider:
  * ``anthropic_native``  — POST /v1/messages
  * ``openai_compatible`` — POST /v1/chat/completions (OpenAI, Azure,
    Gemini's OpenAI-compatible endpoint, any custom server)
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

_CATALOGUE_PATH = "config/premium_llm_catalogue.yaml"
_LATENCY_BUDGET_MS = 12000  # cloud round-trips run slower than local

# A premium LLM must return this shape on the cert prompt.
_CERT_PROMPT = (
    "You are a financial news classifier. Respond ONLY with a JSON object "
    'with keys "sentiment" (positive|negative|neutral) and "confidence" '
    '(0..1). Headline: "Acme Corp posts record earnings, raises guidance."'
)


def _load_yaml(path: str | Path) -> dict[str, Any]:
    try:
        import yaml

        p = Path(path)
        if not p.is_file():
            return {}
        return yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001
        logger.warning("premium_llm | could not read %s: %s", path, exc)
        return {}


def load_provider_catalogue(path: str | Path = _CATALOGUE_PATH) -> list[dict[str, Any]]:
    """Return the supported premium-LLM provider catalogue entries."""
    cfg = _load_yaml(path)
    rows = cfg.get("premium_llm_providers")
    return [dict(r) for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


def find_provider(
    provider_id: str, path: str | Path = _CATALOGUE_PATH
) -> Optional[dict[str, Any]]:
    pid = (provider_id or "").strip().lower()
    for entry in load_provider_catalogue(path):
        if str(entry.get("id", "")).strip().lower() == pid:
            return entry
    return None


def _env(name: str | None) -> str:
    return os.getenv(str(name or ""), "").strip()


def build_premium_llm_view(
    *,
    catalogue_path: str | Path = _CATALOGUE_PATH,
    ai_config_path: str | Path = "config/ai.yaml",
) -> dict[str, Any]:
    """Build the Premium LLM provider-picker view.

    Per provider: whether its API key (and endpoint, where required) is
    configured, and which provider is currently active in `ai.yaml`.
    """
    catalogue = load_provider_catalogue(catalogue_path)
    ai_cfg = _load_yaml(ai_config_path)
    premium = (ai_cfg.get("providers") or {}).get("premium_fallback") or {}
    active_provider = str(premium.get("provider", "")).strip().lower()
    active_model = premium.get("model_name")

    providers: list[dict[str, Any]] = []
    for entry in catalogue:
        pid = str(entry.get("id", "")).strip().lower()
        key_set = bool(_env(entry.get("auth_env")))
        requires_base = bool(entry.get("requires_base_url"))
        base_url_set = (
            bool(_env(entry.get("base_url_env"))) if requires_base
            else bool(entry.get("base_url"))
        )
        configured = key_set and base_url_set
        providers.append(
            {
                "id": pid,
                "label": entry.get("label"),
                "endpoint_type": entry.get("endpoint_type"),
                "auth_env": entry.get("auth_env"),
                "base_url_env": entry.get("base_url_env"),
                "requires_base_url": requires_base,
                "suggested_models": list(entry.get("suggested_models") or []),
                "api_key_configured": key_set,
                "base_url_configured": base_url_set,
                "configured": configured,
                "active": pid == active_provider,
            }
        )
    return {
        "providers": providers,
        "active_provider": active_provider or None,
        "active_model": active_model,
        "premium_enabled": bool(premium.get("enabled", False)),
    }


# ── compatibility certification ───────────────────────────────────────────────


@dataclass
class PremiumCertResult:
    passed: bool
    auth_ok: bool = False
    json_mode_ok: bool = False
    schema_ok: bool = False
    latency_ok: bool = False
    latency_ms: Optional[int] = None
    reason: str = ""
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "auth_ok": self.auth_ok,
            "json_mode_ok": self.json_mode_ok,
            "schema_ok": self.schema_ok,
            "latency_ok": self.latency_ok,
            "latency_ms": self.latency_ms,
            "reason": self.reason,
            "detail": dict(self.detail),
        }


def _resolve_base_url(entry: dict[str, Any]) -> str:
    if entry.get("requires_base_url"):
        return _env(entry.get("base_url_env")).rstrip("/")
    return str(entry.get("base_url", "")).strip().rstrip("/")


def _evaluate_text(raw: str, latency_ms: int) -> PremiumCertResult:
    """Shared scoring of a model's text reply against the cert schema."""
    json_ok = False
    schema_ok = False
    try:
        parsed = json.loads(raw)
        json_ok = isinstance(parsed, dict)
        if json_ok:
            schema_ok = (
                str(parsed.get("sentiment", "")).strip().lower()
                in {"positive", "negative", "neutral"}
                and isinstance(parsed.get("confidence"), (int, float))
            )
    except Exception:  # noqa: BLE001
        json_ok = False
    latency_ok = latency_ms <= _LATENCY_BUDGET_MS
    passed = json_ok and schema_ok and latency_ok
    reason = "passed" if passed else "; ".join(
        x for x in (
            "" if json_ok else "did not return valid JSON",
            "" if schema_ok else "missing required schema keys",
            "" if latency_ok else f"too slow ({latency_ms}ms)",
        ) if x
    )
    return PremiumCertResult(
        passed=passed, auth_ok=True, json_mode_ok=json_ok, schema_ok=schema_ok,
        latency_ok=latency_ok, latency_ms=latency_ms, reason=reason,
        detail={"raw_response": raw[:500]},
    )


async def cert_premium_provider(
    provider_id: str,
    *,
    model: str,
    catalogue_path: str | Path = _CATALOGUE_PATH,
) -> PremiumCertResult:
    """Run the compatibility test for a premium provider/model.

    Verifies auth, structured-JSON output, and latency against the live
    provider API. Credentials are read from the environment — never
    passed in or echoed.
    """
    entry = find_provider(provider_id, catalogue_path)
    if entry is None:
        return PremiumCertResult(passed=False, reason="provider not in catalogue")
    if not str(model or "").strip():
        return PremiumCertResult(passed=False, reason="model id is required")

    api_key = _env(entry.get("auth_env"))
    if not api_key:
        return PremiumCertResult(
            passed=False, reason=f"API key {entry.get('auth_env')} not configured"
        )
    base_url = _resolve_base_url(entry)
    if not base_url:
        return PremiumCertResult(
            passed=False, reason="provider endpoint (base_url) not configured"
        )

    try:
        import httpx
    except Exception as exc:  # noqa: BLE001
        return PremiumCertResult(passed=False, reason=f"httpx unavailable: {exc}")

    endpoint_type = str(entry.get("endpoint_type", "")).strip().lower()
    started = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=_LATENCY_BUDGET_MS / 1000.0 + 8) as client:
            if endpoint_type == "anthropic_native":
                resp = await client.post(
                    f"{base_url}/v1/messages",
                    headers={
                        "x-api-key": api_key,
                        "anthropic-version": "2023-06-01",
                        "content-type": "application/json",
                    },
                    json={
                        "model": model,
                        "max_tokens": 256,
                        "messages": [{"role": "user", "content": _CERT_PROMPT}],
                    },
                )
            else:  # openai_compatible
                resp = await client.post(
                    f"{base_url}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "content-type": "application/json",
                    },
                    json={
                        "model": model,
                        "messages": [{"role": "user", "content": _CERT_PROMPT}],
                        "temperature": 0.0,
                        "max_tokens": 256,
                    },
                )
    except Exception as exc:  # noqa: BLE001
        return PremiumCertResult(passed=False, reason=f"request failed: {exc}")

    latency_ms = int((time.monotonic() - started) * 1000)
    if resp.status_code in (401, 403):
        return PremiumCertResult(
            passed=False, latency_ms=latency_ms,
            reason=f"authentication rejected (HTTP {resp.status_code})",
        )
    if resp.status_code != 200:
        return PremiumCertResult(
            passed=False, auth_ok=True, latency_ms=latency_ms,
            reason=f"provider returned HTTP {resp.status_code}",
        )

    try:
        body = resp.json()
        if endpoint_type == "anthropic_native":
            blocks = body.get("content", [])
            raw = "".join(b.get("text", "") for b in blocks if isinstance(b, dict))
        else:
            raw = body["choices"][0]["message"]["content"]
    except Exception as exc:  # noqa: BLE001
        return PremiumCertResult(
            passed=False, auth_ok=True, latency_ms=latency_ms,
            reason=f"unexpected response shape: {exc}",
        )
    return _evaluate_text(str(raw or ""), latency_ms)


def set_premium_provider(
    provider_id: str,
    model: str,
    *,
    catalogue_path: str | Path = _CATALOGUE_PATH,
    ai_config_path: str | Path = "config/ai.yaml",
) -> bool:
    """Point the Premium LLM stage at a catalogue provider/model (`ai.yaml`).

    Catalogue-only: refuses a provider id not in the supported catalogue.
    """
    entry = find_provider(provider_id, catalogue_path)
    if entry is None or not str(model or "").strip():
        return False
    try:
        import yaml

        p = Path(ai_config_path)
        cfg = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        providers = cfg.setdefault("providers", {})
        premium = providers.setdefault("premium_fallback", {})
        premium["provider"] = str(entry["id"]).strip().lower()
        premium["model_name"] = str(model).strip()
        p.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("premium_llm | could not set provider %s: %s", provider_id, exc)
        return False
