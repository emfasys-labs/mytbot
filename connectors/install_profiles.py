"""
connectors/install_profiles.py
==============================
M11 install-profile recommender.

Reads ``config/install_profiles.yaml`` and the machine probe
(``connectors/machine_probe.py``) to recommend the highest install profile the
operator's hardware comfortably supports — Lite (SQLite, no Docker), Standard
(+ FinBERT + Postgres), or Local AI (+ Ollama local reasoning).

Pure and best-effort: never raises, degrades to Lite when in doubt. Thresholds
come from YAML, not code (no hard-coded numbers in the decision).
"""

from __future__ import annotations

import logging
import os
from typing import Any

import yaml

logger = logging.getLogger(__name__)

_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "config",
    "install_profiles.yaml",
)

_DEFAULT_ORDER = ["local_ai", "standard", "lite"]


def load_profiles(path: str | None = None) -> dict[str, Any]:
    """Load the install-profile catalogue. Returns ``{}`` on any failure."""
    try:
        with open(path or _CONFIG_PATH, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        return data if isinstance(data, dict) else {}
    except Exception as exc:  # noqa: BLE001
        logger.warning("install_profiles | could not load catalogue | {}", exc)
        return {}


def _meets(probe: dict[str, Any], min_specs: dict[str, Any]) -> bool:
    """True when the probe satisfies every declared minimum spec."""
    for key, threshold in (min_specs or {}).items():
        try:
            if float(probe.get(key, 0) or 0) < float(threshold):
                return False
        except (TypeError, ValueError):
            return False
    return True


def _gpu_gate_ok(profile_id: str, probe: dict[str, Any], rec_cfg: dict[str, Any]) -> bool:
    """Local-AI needs a GPU with enough VRAM, unless the box has lots of CPU RAM."""
    if profile_id != "local_ai":
        return True
    try:
        min_vram = float(rec_cfg.get("local_ai_min_vram_gb", 6) or 6)
        cpu_fallback = float(rec_cfg.get("local_ai_cpu_fallback_ram_gb", 32) or 32)
        if bool(probe.get("accelerated")) and float(probe.get("vram_gb", 0) or 0) >= min_vram:
            return True
        return float(probe.get("ram_gb", 0) or 0) >= cpu_fallback
    except (TypeError, ValueError):
        return False


def recommend_profile(
    probe: dict[str, Any],
    catalogue: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Recommend the highest-tier profile the machine supports.

    Returns ``{recommended, reasons, profiles}`` where ``profiles`` maps each
    profile id to ``{eligible, label, summary, ...}``. Falls back to ``lite``.
    """
    cat = catalogue or load_profiles()
    profiles: dict[str, Any] = cat.get("profiles", {}) or {}
    rec_cfg: dict[str, Any] = cat.get("recommendation", {}) or {}
    order: list[str] = rec_cfg.get("order") or _DEFAULT_ORDER

    eligibility: dict[str, bool] = {}
    for pid, spec in profiles.items():
        eligibility[pid] = _meets(probe, spec.get("min_specs", {})) and _gpu_gate_ok(
            pid, probe, rec_cfg
        )

    recommended = "lite"
    for pid in order:
        if eligibility.get(pid):
            recommended = pid
            break

    reasons: list[str] = []
    rspec = profiles.get(recommended, {})
    reasons.append(
        f"ram={probe.get('ram_gb', 0)}GB disk_free={probe.get('disk_free_gb', 0)}GB "
        f"gpu={'yes' if probe.get('accelerated') else 'no'}"
    )
    if recommended == "local_ai":
        reasons.append("hardware supports local reasoning models")
    elif recommended == "standard":
        if not eligibility.get("local_ai"):
            reasons.append("insufficient GPU/RAM/disk for Local AI — Standard recommended")
    else:
        reasons.append("low-spec or constrained machine — Lite is the safest start")

    return {
        "recommended": recommended,
        "reasons": reasons,
        "profiles": {
            pid: {
                "eligible": eligibility.get(pid, False),
                "label": spec.get("label", pid),
                "summary": " ".join((spec.get("summary") or "").split()),
                "requirements": spec.get("requirements"),
                "db_backend": spec.get("db_backend"),
                "docker_required": bool(spec.get("docker_required")),
                "requires_ollama": bool(spec.get("requires_ollama")),
                "min_specs": spec.get("min_specs", {}),
            }
            for pid, spec in profiles.items()
        },
    }
