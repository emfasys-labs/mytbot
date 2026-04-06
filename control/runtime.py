"""
Lightweight runtime registry for shared service instances.
"""

from __future__ import annotations

from typing import Any

_RISK_ENGINE: Any = None
_EXECUTION_ENGINE: Any = None


def set_risk_engine(engine: Any) -> None:
    global _RISK_ENGINE
    _RISK_ENGINE = engine


def get_risk_engine() -> Any:
    return _RISK_ENGINE


def set_execution_engine(engine: Any) -> None:
    global _EXECUTION_ENGINE
    _EXECUTION_ENGINE = engine


def get_execution_engine() -> Any:
    return _EXECUTION_ENGINE

