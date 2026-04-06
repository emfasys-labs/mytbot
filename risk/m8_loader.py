"""Load optional M8 micro-live profile into risk engine config."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from loguru import logger


def merge_m8_into_risk_cfg(risk_cfg: dict[str, Any], m8_path: str | None) -> None:
    """If path exists, merge YAML into `risk_cfg['m8_micro_live']`."""
    if not m8_path:
        return
    p = Path(m8_path)
    if not p.is_file():
        logger.info("risk | M8 profile not found (optional) | {}", p)
        return
    try:
        with p.open(encoding="utf-8") as f:
            raw = yaml.safe_load(f)
        if isinstance(raw, dict):
            risk_cfg["m8_micro_live"] = raw
            logger.info("risk | M8 micro-live profile loaded | {}", p)
    except OSError as exc:
        logger.warning("risk | M8 profile unreadable | {} | {}", p, exc)
