"""
connectors/local_llm.py
========================
D127 Connect Hub v2 — Phase 4: Local LLM catalogue, fitness, install, cert.

The Local LLM stage uses a **curated catalogue** of supported models
(`config/local_llm_catalogue.yaml`). The machine probe recommends the
best-fit model; weak machines gracefully skip the stage entirely.

A catalogue model only becomes usable after a **compatibility
certification** — a JSON-mode test, a latency test, and a
schema-conformance test against the live Ollama runtime. The actual
trading path keeps running on Rules + FinBERT (+ Premium) when no local
model is available.

Open-decision defaults taken in P4:
  * Weak machine → **silent skip** (state recorded, shown as
    `unavailable` in the UI). No launch-time prompt.
  * **Catalogue-only** — the custom-model Experimental escape hatch is
    deferred; only tested catalogue models can be installed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

_CATALOGUE_PATH = "config/local_llm_catalogue.yaml"

# Fitness verdicts.
RECOMMENDED = "recommended"
AVAILABLE = "available"
TOO_SLOW = "too_slow"
UNSUPPORTED = "unsupported"

_DISK_HEADROOM_GB = 2.0


def _load_yaml(path: str | Path) -> dict[str, Any]:
    try:
        import yaml

        p = Path(path)
        if not p.is_file():
            return {}
        return yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001
        logger.warning("local_llm | could not read %s: %s", path, exc)
        return {}


def load_catalogue(path: str | Path = _CATALOGUE_PATH) -> list[dict[str, Any]]:
    """Return the supported local-LLM catalogue entries."""
    cfg = _load_yaml(path)
    rows = cfg.get("local_llm_catalogue")
    return [dict(r) for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


def _params_billions(entry: dict[str, Any]) -> float:
    raw = str(entry.get("params", "")).strip().lower().replace("b", "")
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


def compute_fitness(probe: dict[str, Any], entry: dict[str, Any]) -> str:
    """Classify how well a catalogue model fits the probed machine.

    Returns one of: `available`, `too_slow`, `unsupported`. The
    `recommended` verdict is assigned separately by `build_local_llm_view`
    (only one model is the recommendation).
    """
    disk_free = float(probe.get("disk_free_gb", 0) or 0)
    if disk_free < float(entry.get("disk_gb", 0) or 0) + _DISK_HEADROOM_GB:
        return UNSUPPORTED

    ram = float(probe.get("ram_gb", 0) or 0)
    vram = float(probe.get("vram_gb", 0) or 0)
    min_ram = float(entry.get("min_ram_gb", 0) or 0)
    min_vram = float(entry.get("min_vram_gb", 0) or 0)
    large = _params_billions(entry) >= 13.0

    if probe.get("accelerated"):
        if vram >= min_vram:
            return AVAILABLE
        # GPU present but VRAM short — may spill to CPU.
        if ram >= min_ram:
            return TOO_SLOW if large else AVAILABLE
        return UNSUPPORTED

    # CPU-only machine.
    if ram >= min_ram:
        # Large models are not viable for live-cadence scoring on CPU.
        return TOO_SLOW if large else AVAILABLE
    return UNSUPPORTED


def recommend_model(
    probe: dict[str, Any], catalogue: list[dict[str, Any]]
) -> Optional[str]:
    """Pick the highest-quality `available` model for the probed machine."""
    best: Optional[dict[str, Any]] = None
    for entry in catalogue:
        if compute_fitness(probe, entry) != AVAILABLE:
            continue
        if best is None or int(entry.get("quality_rank", 0)) > int(best.get("quality_rank", 0)):
            best = entry
    return str(best["id"]) if best else None


def resolve_local_llm_availability(
    probe: dict[str, Any], catalogue: list[dict[str, Any]]
) -> tuple[bool, str]:
    """Decide whether the Local LLM stage can run at all on this machine.

    When False, the operator-facing state is `unavailable` and the AI
    pipeline runs on Rules + FinBERT (+ Premium) — a silent, graceful
    skip, not an error.
    """
    if not probe.get("ollama_available"):
        return (False, "Ollama is not installed or not running on this machine.")
    runnable = [
        e for e in catalogue
        if compute_fitness(probe, e) in (AVAILABLE, TOO_SLOW)
    ]
    if not runnable:
        return (False, "No supported local model fits this machine's hardware.")
    return (True, "")


def build_local_llm_view(
    *,
    probe: Optional[dict[str, Any]] = None,
    catalogue_path: str | Path = _CATALOGUE_PATH,
    installed_models: Optional[set[str]] = None,
) -> dict[str, Any]:
    """Build the Local LLM catalogue screen: per-model fitness + recommendation."""
    if probe is None:
        from connectors.machine_probe import probe_machine

        probe = probe_machine()
    catalogue = load_catalogue(catalogue_path)
    installed = {m.strip().lower() for m in (installed_models or set())}

    recommended_id = recommend_model(probe, catalogue)
    available, reason = resolve_local_llm_availability(probe, catalogue)

    models: list[dict[str, Any]] = []
    for entry in catalogue:
        fitness = compute_fitness(probe, entry)
        mid = str(entry.get("id", "")).strip().lower()
        if mid == (recommended_id or "") and fitness == AVAILABLE:
            fitness = RECOMMENDED
        models.append(
            {
                "id": entry.get("id"),
                "label": entry.get("label"),
                "params": entry.get("params"),
                "disk_gb": entry.get("disk_gb"),
                "min_ram_gb": entry.get("min_ram_gb"),
                "min_vram_gb": entry.get("min_vram_gb"),
                "quality_rank": entry.get("quality_rank"),
                "notes": entry.get("notes"),
                "fitness": fitness,
                "installed": mid in installed,
            }
        )

    return {
        "machine_probe": probe,
        "models": models,
        "recommended_model": recommended_id,
        "local_llm_available": available,
        "unavailable_reason": reason,
    }


# ── installation + compatibility certification ────────────────────────────────


@dataclass
class CertResult:
    """Outcome of the Local LLM compatibility certification."""

    passed: bool
    json_mode_ok: bool = False
    schema_ok: bool = False
    latency_ok: bool = False
    latency_ms: Optional[int] = None
    reason: str = ""
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "json_mode_ok": self.json_mode_ok,
            "schema_ok": self.schema_ok,
            "latency_ok": self.latency_ok,
            "latency_ms": self.latency_ms,
            "reason": self.reason,
            "detail": dict(self.detail),
        }


# A live news-scoring model must return this shape. The cert prompt asks
# for exactly these keys so the schema test is meaningful.
_CERT_PROMPT = (
    "You are a financial news classifier. Respond ONLY with a JSON object "
    'with keys "sentiment" (one of positive, negative, neutral) and '
    '"confidence" (a number 0..1). Headline: '
    '"Acme Corp reports record quarterly earnings, raises guidance."'
)
_LATENCY_BUDGET_MS = 8000


async def cert_local_model(
    model_id: str,
    *,
    ollama_url: str = "http://localhost:11434",
    latency_budget_ms: int = _LATENCY_BUDGET_MS,
) -> CertResult:
    """Run the compatibility certification for a local model.

    Three gates: (1) the model returns parseable JSON, (2) that JSON
    carries the required schema keys, (3) it does so within the latency
    budget. A model must pass all three before it can be `Activated`.
    """
    try:
        import httpx
    except Exception as exc:  # noqa: BLE001
        return CertResult(passed=False, reason=f"httpx unavailable: {exc}")

    payload = {
        "model": model_id,
        "prompt": _CERT_PROMPT,
        "stream": False,
        "format": "json",
        "options": {"temperature": 0.0},
    }
    started = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=max(10.0, latency_budget_ms / 1000.0 + 5)) as client:
            resp = await client.post(f"{ollama_url.rstrip('/')}/api/generate", json=payload)
    except Exception as exc:  # noqa: BLE001
        return CertResult(passed=False, reason=f"Ollama request failed: {exc}")

    latency_ms = int((time.monotonic() - started) * 1000)
    if resp.status_code != 200:
        return CertResult(
            passed=False, latency_ms=latency_ms,
            reason=f"Ollama returned HTTP {resp.status_code}",
        )

    try:
        body = resp.json()
        raw = str(body.get("response", ""))
    except Exception as exc:  # noqa: BLE001
        return CertResult(passed=False, latency_ms=latency_ms,
                          reason=f"unparseable Ollama envelope: {exc}")

    json_ok = False
    schema_ok = False
    parsed: dict[str, Any] = {}
    try:
        parsed = json.loads(raw)
        json_ok = isinstance(parsed, dict)
    except Exception:  # noqa: BLE001
        json_ok = False

    if json_ok:
        has_sentiment = str(parsed.get("sentiment", "")).strip().lower() in {
            "positive", "negative", "neutral"
        }
        has_conf = isinstance(parsed.get("confidence"), (int, float))
        schema_ok = has_sentiment and has_conf

    latency_ok = latency_ms <= latency_budget_ms
    passed = json_ok and schema_ok and latency_ok
    reason = "passed" if passed else "; ".join(
        x for x in (
            "" if json_ok else "did not return valid JSON",
            "" if schema_ok else "missing required schema keys",
            "" if latency_ok else f"too slow ({latency_ms}ms > {latency_budget_ms}ms)",
        ) if x
    )
    return CertResult(
        passed=passed,
        json_mode_ok=json_ok,
        schema_ok=schema_ok,
        latency_ok=latency_ok,
        latency_ms=latency_ms,
        reason=reason,
        detail={"raw_response": raw[:500]},
    )


@dataclass
class InstallResult:
    ok: bool
    model_id: str
    pulled: bool = False
    cert: Optional[CertResult] = None
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "model_id": self.model_id,
            "pulled": self.pulled,
            "cert": self.cert.to_dict() if self.cert else None,
            "reason": self.reason,
        }


async def install_local_model(
    model_id: str,
    *,
    ollama_url: str = "http://localhost:11434",
    catalogue_path: str | Path = _CATALOGUE_PATH,
    pull_timeout_sec: float = 1800.0,
) -> InstallResult:
    """Download a catalogue model via Ollama, then run its compatibility cert.

    Catalogue-only: a model id that is not in the supported catalogue is
    refused outright.
    """
    mid = (model_id or "").strip().lower()
    catalogue_ids = {str(e.get("id", "")).strip().lower() for e in load_catalogue(catalogue_path)}
    if mid not in catalogue_ids:
        return InstallResult(
            ok=False, model_id=model_id,
            reason="model is not in the supported catalogue",
        )

    # `ollama pull` — the heavy download.
    try:
        proc = await asyncio.create_subprocess_exec(
            "ollama", "pull", mid,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except FileNotFoundError:
        return InstallResult(ok=False, model_id=model_id,
                             reason="the `ollama` binary was not found on PATH")
    except Exception as exc:  # noqa: BLE001
        return InstallResult(ok=False, model_id=model_id,
                             reason=f"could not start `ollama pull`: {exc}")
    try:
        await asyncio.wait_for(proc.communicate(), timeout=pull_timeout_sec)
    except asyncio.TimeoutError:
        proc.kill()
        return InstallResult(ok=False, model_id=model_id,
                             reason=f"`ollama pull` exceeded {pull_timeout_sec:.0f}s")
    if proc.returncode != 0:
        return InstallResult(ok=False, model_id=model_id,
                             reason=f"`ollama pull` failed (exit {proc.returncode})")

    cert = await cert_local_model(mid, ollama_url=ollama_url)
    return InstallResult(
        ok=cert.passed,
        model_id=mid,
        pulled=True,
        cert=cert,
        reason="installed and certified" if cert.passed else f"certification failed: {cert.reason}",
    )


def set_local_llm_model(
    model_id: str, *, ai_config_path: str | Path = "config/ai.yaml"
) -> bool:
    """Point the Local LLM stage at a catalogue model (writes `ai.yaml`).

    Catalogue-only: refuses a model id that is not in the supported
    catalogue. Returns True when `ai.yaml` was updated.
    """
    mid = (model_id or "").strip().lower()
    catalogue_ids = {str(e.get("id", "")).strip().lower() for e in load_catalogue()}
    if mid not in catalogue_ids:
        return False
    try:
        import yaml

        p = Path(ai_config_path)
        cfg = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        providers = cfg.setdefault("providers", {})
        local = providers.setdefault("local_reasoning", {})
        local["model_name"] = mid
        p.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("local_llm | could not activate model %s: %s", mid, exc)
        return False


async def list_installed_models(
    ollama_url: str = "http://localhost:11434",
) -> set[str]:
    """Return the set of model ids currently installed in Ollama."""
    try:
        import httpx

        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{ollama_url.rstrip('/')}/api/tags")
        if resp.status_code != 200:
            return set()
        data = resp.json()
        return {
            str(m.get("name", "")).strip().lower()
            for m in data.get("models", [])
            if isinstance(m, dict)
        }
    except Exception:  # noqa: BLE001
        return set()
