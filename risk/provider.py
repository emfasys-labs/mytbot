"""
Unified parameter provider with precedence and staleness guards.

Priority:
1) Live runtime values
2) Derived/model values
3) ParameterManager defaults/overrides
4) Operational config fallback
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from loguru import logger

from risk.parameters import ParameterManager


@dataclass
class _Snapshot:
    value: Decimal
    updated_at: datetime
    source: str


class ParameterProvider:
    def __init__(
        self,
        parameter_manager: ParameterManager,
        operational_config: dict[str, Any] | None = None,
        staleness_seconds: int = 300,
    ):
        self._pm = parameter_manager
        self._cfg = operational_config or {}
        self._live: dict[str, _Snapshot] = {}
        self._derived: dict[str, _Snapshot] = {}
        self._staleness = timedelta(seconds=max(1, staleness_seconds))

    def set_live(self, key: str, value: Decimal | str | float | int) -> None:
        self._live[key] = _Snapshot(
            value=Decimal(str(value)),
            updated_at=datetime.now(timezone.utc),
            source="live",
        )

    def set_derived(self, key: str, value: Decimal | str | float | int) -> None:
        self._derived[key] = _Snapshot(
            value=Decimal(str(value)),
            updated_at=datetime.now(timezone.utc),
            source="derived",
        )

    def get_decimal(self, key: str, config_fallback_key: str | None = None, fallback: Decimal | None = None) -> Decimal:
        now = datetime.now(timezone.utc)
        live = self._live.get(key)
        if live and (now - live.updated_at) <= self._staleness:
            return live.value
        if live:
            logger.warning("provider | stale live value ignored | {}", key)

        derived = self._derived.get(key)
        if derived and (now - derived.updated_at) <= self._staleness:
            return derived.value
        if derived:
            logger.warning("provider | stale derived value ignored | {}", key)

        try:
            return self._pm.get_value(key)
        except Exception:  # noqa: BLE001
            pass

        cfg_key = config_fallback_key or key
        if cfg_key in self._cfg:
            return Decimal(str(self._cfg[cfg_key]))
        if fallback is not None:
            return fallback
        raise KeyError(f"Missing parameter: {key}")
