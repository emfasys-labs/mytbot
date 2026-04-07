from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Iterable

from loguru import logger


@dataclass(frozen=True)
class ValidationResult:
    ok: bool
    missing: list[str]
    warnings: list[str]


def _is_test_env() -> bool:
    return bool(os.getenv("PYTEST_CURRENT_TEST"))


def _missing_env(keys: Iterable[str]) -> list[str]:
    missing: list[str] = []
    for k in keys:
        if not (os.getenv(k, "").strip()):
            missing.append(k)
    return missing


def validate_startup_env(
    *,
    component: str,
    require_postgres: bool = True,
    require_ibkr: bool = False,
    require_kraken: bool = False,
    require_binance: bool = False,
    require_ai: bool = False,
    strict: bool = True,
) -> ValidationResult:
    missing: list[str] = []
    warnings: list[str] = []

    if require_postgres:
        missing.extend(
            _missing_env(
                (
                    "POSTGRES_HOST",
                    "POSTGRES_PORT",
                    "POSTGRES_DB",
                    "POSTGRES_USER",
                    "POSTGRES_PASSWORD",
                )
            )
        )
    if require_ibkr:
        missing.extend(_missing_env(("IBKR_HOST", "IBKR_PORT")))
    if require_kraken:
        missing.extend(_missing_env(("KRAKEN_API_KEY", "KRAKEN_API_SECRET")))
    if require_binance:
        missing.extend(_missing_env(("BINANCE_API_KEY", "BINANCE_API_SECRET")))
    if require_ai:
        missing.extend(_missing_env(("ANTHROPIC_API_KEY",)))

    if not require_postgres:
        warnings.append("Postgres checks disabled for this component.")
    if _is_test_env():
        warnings.append("Running in test env; startup env validation is non-fatal.")

    ok = len(missing) == 0
    if ok:
        logger.info("startup validation | {} | OK", component)
        return ValidationResult(ok=True, missing=[], warnings=warnings)

    msg = f"startup validation failed | {component} | missing env: {', '.join(sorted(set(missing)))}"
    if strict and not _is_test_env():
        raise RuntimeError(msg)
    logger.warning(msg)
    return ValidationResult(ok=False, missing=missing, warnings=warnings)
