"""Structured, JSON-friendly arbitrage / global-edge log lines (key=value for aggregation)."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from loguru import logger


def _fmt(d: dict[str, Any]) -> str:
    parts = []
    for k, v in sorted(d.items()):
        if isinstance(v, Decimal):
            parts.append(f"{k}={str(v)}")
        else:
            parts.append(f"{k}={v!r}")
    return " ".join(parts)


def log_arb_event(phase: str, **fields: Any) -> None:
    """Emit one line with event=arb_global and stable keys."""
    payload = {"event": "arb_global", "phase": phase, **fields}
    logger.info(_fmt(payload))
