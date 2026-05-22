"""
connectors/ai_pipeline.py
==========================
D127 Connect Hub v2 — Phase 3: the AI pipeline as four managed stages.

Unlike brokers and feeds (collections you add/remove), the AI pipeline is
**four fixed stages** in escalation order:

    1. Rules Engine      — deterministic core; never disable, never delete
    2. FinBERT Sentiment — never delete; disable only if another sentiment
                           provider is active
    3. Local LLM         — never delete; disable allowed
    4. Premium LLM       — never delete; disable allowed

You configure / enable / disable / version a stage; you never add or
remove one. This module builds the descriptor the Connect screen renders
and is the single authority for the per-stage enable/disable rules.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Fixed escalation order. The keys match `config/ai.yaml::providers` and
# `config/connectors.yaml::ai_providers`.
STAGE_ORDER: tuple[str, ...] = (
    "rules",
    "fin_sentiment",
    "local_reasoning",
    "premium_fallback",
)

_STAGE_META: dict[str, dict[str, Any]] = {
    "rules": {
        "label": "Rules Engine",
        "role": "fast_classifier",
        "core": True,
        "summary": "Deterministic keyword/materiality baseline. Always on.",
    },
    "fin_sentiment": {
        "label": "FinBERT Sentiment",
        "role": "sentiment_classifier",
        "core": False,
        "summary": "Thin, version-pinned financial-sentiment model.",
    },
    "local_reasoning": {
        "label": "Local LLM",
        "role": "reasoning_model",
        "core": False,
        "summary": "Optional local reasoning model (Ollama).",
    },
    "premium_fallback": {
        "label": "Premium LLM",
        "role": "premium_arbiter",
        "core": False,
        "summary": "Optional paid escalation/arbiter model. Advises only.",
    },
}


def _load_yaml(path: str | Path) -> dict[str, Any]:
    try:
        import yaml

        p = Path(path)
        if not p.is_file():
            return {}
        return yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001
        logger.warning("ai_pipeline | could not read %s: %s", path, exc)
        return {}


def _ai_providers(ai_config_path: str | Path) -> dict[str, dict[str, Any]]:
    cfg = _load_yaml(ai_config_path)
    providers = cfg.get("providers")
    return providers if isinstance(providers, dict) else {}


def can_disable_ai_stage(
    stage_id: str,
    *,
    ai_config_path: str | Path = "config/ai.yaml",
) -> tuple[bool, str]:
    """Return ``(can_disable, blocked_reason)`` for an AI pipeline stage.

    Rules:
      * `rules` — never disableable (deterministic core).
      * `fin_sentiment` — disableable only if another *enabled* provider
        also carries the `sentiment_classifier` role.
      * `local_reasoning` / `premium_fallback` — always disableable.

    No AI stage is ever deletable (they are pipeline stages, not
    connectors) — that is enforced separately.
    """
    sid = (stage_id or "").strip().lower()
    if sid == "rules":
        return (False, "Rules Engine is the deterministic core and cannot be disabled.")

    if sid == "fin_sentiment":
        providers = _ai_providers(ai_config_path)
        # Another sentiment provider = a different enabled provider whose
        # Connect Hub manifest carries the sentiment_classifier role.
        try:
            from system.connect_hub import load_connector_manifests

            sentiment_ids = {
                m.id
                for m in load_connector_manifests()
                if m.category == "ai_providers" and "sentiment_classifier" in m.roles
            }
        except Exception:  # noqa: BLE001
            sentiment_ids = {"fin_sentiment"}
        others = [
            pid
            for pid in sentiment_ids
            if pid != "fin_sentiment"
            and bool((providers.get(pid) or {}).get("enabled", False))
        ]
        if others:
            return (True, "")
        return (
            False,
            "FinBERT is the only active sentiment provider; enable another "
            "before disabling it.",
        )

    if sid in ("local_reasoning", "premium_fallback"):
        return (True, "")

    return (False, f"Unknown AI pipeline stage '{stage_id}'.")


def _stage_model_info(stage_id: str, provider_cfg: dict[str, Any]) -> dict[str, Any]:
    """Per-stage model / version detail surfaced on the stage card."""
    sid = stage_id
    if sid == "fin_sentiment":
        return {
            "model_name": provider_cfg.get("model_name", "ProsusAI/finbert"),
            # FinBERT is a pinned pretrained checkpoint. `version` is a
            # logical label; `model_revision` pins the HF checkpoint.
            "version": str(provider_cfg.get("version", "1.0.0")),
            "model_revision": provider_cfg.get("model_revision"),
            "device": provider_cfg.get("device", "auto"),
        }
    if sid == "local_reasoning":
        return {
            "provider": provider_cfg.get("provider", "ollama"),
            "model_name": provider_cfg.get("model_name"),
            "fallback_model": provider_cfg.get("fallback_model"),
            "base_url": provider_cfg.get("base_url"),
        }
    if sid == "premium_fallback":
        return {
            "provider": provider_cfg.get("provider"),
            "model_name": provider_cfg.get("model_name"),
        }
    return {}


def build_ai_pipeline_view(
    *,
    ai_config_path: str | Path = "config/ai.yaml",
) -> dict[str, Any]:
    """Build the four-stage AI pipeline descriptor for the Connect screen."""
    providers = _ai_providers(ai_config_path)
    stages: list[dict[str, Any]] = []
    for order, stage_id in enumerate(STAGE_ORDER, start=1):
        meta = _STAGE_META[stage_id]
        pcfg = providers.get(stage_id) or {}
        enabled = bool(pcfg.get("enabled", False))
        can_disable, blocked_reason = can_disable_ai_stage(
            stage_id, ai_config_path=ai_config_path
        )
        stages.append(
            {
                "id": stage_id,
                "label": meta["label"],
                "role": meta["role"],
                "order": order,
                "core": meta["core"],
                "summary": meta["summary"],
                "enabled": enabled,
                "can_disable": can_disable,
                "disable_blocked_reason": blocked_reason,
                "can_delete": False,  # stages are never deletable
                "model": _stage_model_info(stage_id, pcfg),
            }
        )
    return {
        "stages": stages,
        "stage_count": len(stages),
        "enabled_count": sum(1 for s in stages if s["enabled"]),
    }
