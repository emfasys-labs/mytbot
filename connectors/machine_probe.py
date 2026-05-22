"""
connectors/machine_probe.py
============================
D127 Connect Hub v2 — Phase 4: machine hardware probe.

Detects what the operator's machine can run, so the Local LLM catalogue
can recommend a best-fit model and skip the stage entirely on hardware
too weak to run any model usefully.

The probe is best-effort and never raises — every field degrades to a
safe default if its detection path is unavailable.
"""

from __future__ import annotations

import logging
import os
import shutil
from typing import Any

logger = logging.getLogger(__name__)


def _cpu_count() -> int:
    try:
        return int(os.cpu_count() or 0)
    except Exception:  # noqa: BLE001
        return 0


def _ram_gb() -> float:
    try:
        import psutil

        return round(psutil.virtual_memory().total / 1e9, 1)
    except Exception:  # noqa: BLE001
        return 0.0


def _disk_free_gb(path: str = ".") -> float:
    try:
        return round(shutil.disk_usage(path).free / 1e9, 1)
    except Exception:  # noqa: BLE001
        return 0.0


def _gpu_info() -> tuple[bool, str | None, float]:
    """Return (gpu_present, gpu_name, vram_gb) — best-effort via torch.cuda."""
    try:
        import torch  # FinBERT already pulls torch into the env

        if not torch.cuda.is_available():
            return (False, None, 0.0)
        props = torch.cuda.get_device_properties(0)
        return (True, str(props.name), round(props.total_memory / 1e9, 1))
    except Exception:  # noqa: BLE001
        return (False, None, 0.0)


def _ollama_available(base_url: str = "http://localhost:11434") -> bool:
    """True when an Ollama daemon answers on the configured port."""
    # Cheap binary-on-PATH check first.
    if shutil.which("ollama") is None:
        # Binary absent — still try the HTTP probe in case it runs elsewhere.
        pass
    try:
        import httpx

        resp = httpx.get(f"{base_url.rstrip('/')}/api/tags", timeout=2.0)
        return resp.status_code == 200
    except Exception:  # noqa: BLE001
        return shutil.which("ollama") is not None


def probe_machine(*, ollama_url: str = "http://localhost:11434") -> dict[str, Any]:
    """Return a hardware-capability snapshot.

    Keys: cpu_count, ram_gb, gpu_present, gpu_name, vram_gb,
    disk_free_gb, ollama_available, ollama_url, accelerated.
    """
    gpu_present, gpu_name, vram_gb = _gpu_info()
    return {
        "cpu_count": _cpu_count(),
        "ram_gb": _ram_gb(),
        "gpu_present": gpu_present,
        "gpu_name": gpu_name,
        "vram_gb": vram_gb,
        "disk_free_gb": _disk_free_gb(),
        "ollama_available": _ollama_available(ollama_url),
        "ollama_url": ollama_url,
        # `accelerated` = a usable GPU is present; CPU-only machines run
        # local models far slower and the catalogue narrows accordingly.
        "accelerated": gpu_present and vram_gb > 0,
    }
