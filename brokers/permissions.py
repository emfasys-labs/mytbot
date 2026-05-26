"""
Runtime broker permission loader and checks.
"""

from __future__ import annotations

import logging
from pathlib import Path
from threading import RLock
from typing import Any

import yaml

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "broker_permissions.yaml"


class BrokerPermissions:
    def __init__(self, config_path: str | Path | None = None):
        self.config_path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
        self._lock = RLock()
        self._mtime_ns: int | None = None
        self._matrix: dict[str, dict[str, dict[str, Any]]] = {}
        self.raw_config: dict[str, Any] = {}
        self.reload(force=True)

    def reload(self, *, force: bool = False) -> None:
        with self._lock:
            if not self.config_path.exists():
                if force:
                    logger.warning(
                        "broker_permissions | config missing | %s | all permissions default to enabled",
                        self.config_path,
                    )
                self._matrix = {}
                self._mtime_ns = None
                return

            stat = self.config_path.stat()
            if not force and self._mtime_ns == stat.st_mtime_ns:
                return

            with self.config_path.open(encoding="utf-8") as f:
                raw = yaml.safe_load(f) or {}
            if not isinstance(raw, dict):
                logger.warning(
                    "broker_permissions | invalid format in %s | expected map root",
                    self.config_path,
                )
                raw = {}

            normalized: dict[str, dict[str, dict[str, Any]]] = {}
            for broker, perms in raw.items():
                if not isinstance(perms, dict):
                    continue
                b = str(broker).strip().lower()
                normalized[b] = {}
                for asset, state in perms.items():
                    a = str(asset).strip().lower()
                    if isinstance(state, dict):
                        enabled = bool(state.get("enabled", True))
                        reason = state.get("reason")
                        restored_after = state.get("restored_after")
                    else:
                        enabled = bool(state)
                        reason = None
                        restored_after = None
                    normalized[b][a] = {
                        "enabled": enabled,
                        "reason": reason,
                        "restored_after": restored_after,
                    }
            self.raw_config = raw
            self._matrix = normalized
            self._mtime_ns = stat.st_mtime_ns
            logger.info("broker_permissions | reloaded | brokers=%s", len(self._matrix))

    def get_taker_fee_bps(self, broker: str) -> float:
        self.reload()
        b = broker.strip().lower()
        return float(self.raw_config.get(b, {}).get("taker_fee_bps", 0.0))

    def get_borrow_rate_annual_pct(self, broker: str) -> float:
        self.reload()
        b = broker.strip().lower()
        return float(self.raw_config.get(b, {}).get("borrow_rate_annual_pct", 0.0))


    def check_permission(self, broker: str, asset_class: str) -> bool:
        self.reload()
        b = broker.strip().lower()
        a = asset_class.strip().lower()
        state = self._matrix.get(b, {}).get(a)
        if state is None:
            return True
        ok = bool(state.get("enabled", True))
        if not ok:
            logger.warning(
                "broker_permissions | disabled | broker=%s asset_class=%s reason=%s restored_after=%s",
                b,
                a,
                state.get("reason"),
                state.get("restored_after"),
            )
        return ok

    def get_fallback_broker(
        self,
        asset_class: str,
        *,
        candidates: list[str],
        exclude: list[str] | None = None,
    ) -> str | None:
        self.reload()
        ex = {x.strip().lower() for x in (exclude or [])}
        for broker in candidates:
            b = broker.strip().lower()
            if b in ex:
                continue
            if self.check_permission(b, asset_class):
                return b
        return None


_GLOBAL_PERMISSIONS = BrokerPermissions()


def get_permissions() -> BrokerPermissions:
    return _GLOBAL_PERMISSIONS

