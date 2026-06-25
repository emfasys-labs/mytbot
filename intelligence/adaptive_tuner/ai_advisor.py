"""AI advisor for the Adaptive Tuner (point 3 — AI in the tuning loop).

The advisor is OPTIONAL and purely advisory (rule 7): it never sets a value. It
is given a grounded, retrieved context of myTbot's own recent behaviour — the
reward trend, the current parameter values and their hard bounds, each
parameter's best-observed value per regime, and a summary of recent fills — and
returns a soft *direction* hint per parameter ("up"/"down"/"hold") plus a
one-line rationale. The bounded statistical optimizer always decides the actual
magnitude, clamped to [min, max].

Uses the OpenAI-compatible ``local_reasoning`` endpoint from ``config/ai.yaml``
(Gemini by default). Any failure (rate limit, unreachable, bad JSON) → empty
hints, so the tuner runs fully on statistics alone.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

import httpx
import yaml
from loguru import logger

_HINTS = {"up", "down", "hold"}


def _load_local_reasoning_cfg() -> dict[str, Any]:
    try:
        raw = yaml.safe_load(Path("config/ai.yaml").read_text(encoding="utf-8")) or {}
        return (raw.get("providers") or {}).get("local_reasoning") or {}
    except Exception:  # noqa: BLE001
        return {}


class TunerAIAdvisor:
    def __init__(self, cfg: dict[str, Any] | None = None):
        cfg = cfg or _load_local_reasoning_cfg()
        self.base_url = str(cfg.get("base_url", "") or "").rstrip("/")
        self.model = str(cfg.get("model_name") or cfg.get("model") or "gemini-2.5-flash")
        key_env = str(cfg.get("api_key_env", "") or "").strip()
        self.api_key = str(cfg.get("api_key", "") or "").strip() or (
            os.getenv(key_env, "") if key_env else os.getenv("LOCAL_REASONING_API_KEY", "")
        )
        try:
            self.timeout = float(cfg.get("timeout", 30) or 30)
        except (TypeError, ValueError):
            self.timeout = 30.0

    @property
    def available(self) -> bool:
        return bool(self.base_url and "/v1" in self.base_url and self.api_key)

    async def advise(self, context: dict[str, Any]) -> dict[str, str]:
        """Return ``{param_key: "up"/"down"/"hold"}`` plus ``{"_rationale": str}``."""
        if not self.available:
            return {}
        prompt = self._build_prompt(context)
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(
                    f"{self.base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json={
                        "model": self.model,
                        "messages": [
                            {
                                "role": "system",
                                "content": (
                                    "You are a quantitative risk analyst tuning a live trading "
                                    "system. Reply with STRICT JSON only."
                                ),
                            },
                            {"role": "user", "content": prompt},
                        ],
                        "temperature": 0.2,
                        "max_tokens": 1024,
                    },
                )
            if resp.status_code != 200:
                logger.debug("tuner_ai | endpoint {} → no hints", resp.status_code)
                return {}
            content = resp.json()["choices"][0]["message"]["content"]
            return self._parse(content, context)
        except Exception as exc:  # noqa: BLE001
            logger.debug("tuner_ai | advisor failed | {}", exc)
            return {}

    def _build_prompt(self, ctx: dict[str, Any]) -> str:
        params = ctx.get("params", [])
        lines = [
            "Decide a tuning DIRECTION for each parameter of a multi-strategy trading book.",
            f"Current market regime: {ctx.get('regime', 'unknown')}.",
            f"Recent reward (net realized P&L / NAV over the window): {ctx.get('reward'):.5f}.",
            f"Reward trend (last cycles): {ctx.get('reward_trend')}.",
            f"Recent fills summary: {ctx.get('fills_summary')}.",
            "",
            "Parameters (name | current | [min,max] | best-observed value so far):",
        ]
        for p in params:
            lines.append(
                f"- {p['key']} | cur={p['current']} | [{p['min']},{p['max']}] | best={p.get('best')}"
            )
        lines += [
            "",
            "Higher entry_conviction_threshold = fewer/stronger trades; higher "
            "concentration_exponent = more concentrated; higher gross/net/position "
            "caps = more aggressive deployment.",
            "If recent reward is negative, prefer de-risking directions; if positive "
            "and stable, you may lean into deployment.",
            'Reply ONLY as JSON: {"hints": {"<param_key>": "up|down|hold", ...}, '
            '"rationale": "<one sentence>"}.',
        ]
        return "\n".join(lines)

    def _parse(self, content: str, ctx: dict[str, Any]) -> dict[str, str]:
        valid_keys = {p["key"] for p in ctx.get("params", [])}
        try:
            m = re.search(r"\{.*\}", content, re.DOTALL)
            data = json.loads(m.group(0) if m else content)
        except Exception:  # noqa: BLE001
            return {}
        hints_raw = data.get("hints") if isinstance(data, dict) else None
        out: dict[str, str] = {}
        if isinstance(hints_raw, dict):
            for k, v in hints_raw.items():
                if k in valid_keys and str(v).strip().lower() in _HINTS:
                    out[k] = str(v).strip().lower()
        rationale = data.get("rationale") if isinstance(data, dict) else None
        if rationale:
            out["_rationale"] = str(rationale)[:500]
        return out
