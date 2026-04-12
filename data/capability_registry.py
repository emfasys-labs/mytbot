from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Optional


@dataclass(frozen=True)
class BrokerCapabilities:
    """Declarative venue capabilities for broker-agnostic arbitrage (not part of ``BrokerAdapter`` ABC)."""

    name: str

    supports_spot: bool
    supports_perpetuals: bool
    supports_options: bool
    supports_margin: bool
    supports_shorting: bool

    supported_symbols: frozenset[str]

    fee_bps: Decimal
    latency_ms: int
    liquidity_score: Decimal


def _parse_capabilities_row(name: str, row: dict[str, Any]) -> BrokerCapabilities:
    syms = row.get("supported_symbols") or []
    if not isinstance(syms, list):
        syms = []
    return BrokerCapabilities(
        name=str(row.get("name", name)).strip().lower() or name.strip().lower(),
        supports_spot=bool(row.get("supports_spot", False)),
        supports_perpetuals=bool(row.get("supports_perpetuals", False)),
        supports_options=bool(row.get("supports_options", False)),
        supports_margin=bool(row.get("supports_margin", False)),
        supports_shorting=bool(row.get("supports_shorting", False)),
        supported_symbols=frozenset(str(s).strip().upper() for s in syms if str(s).strip()),
        fee_bps=Decimal(str(row.get("fee_bps", "0"))),
        latency_ms=int(row.get("latency_ms", 999)),
        liquidity_score=Decimal(str(row.get("liquidity_score", "0.5"))),
    )


class CapabilityRegistry:
    """
    Aggregates broker capability records (from config) for venue pairing.
    Adapters stay unchanged; this layer is data-driven so new venues are YAML-only.
    """

    def __init__(self, logger: Any | None = None) -> None:
        self._capabilities: Dict[str, BrokerCapabilities] = {}
        self._logger = logger

    def load_from_config(self, cfg: dict[str, Any] | None) -> None:
        self._capabilities.clear()
        if not isinstance(cfg, dict):
            return
        brokers = (cfg.get("brokers") or {}) if isinstance(cfg.get("brokers"), dict) else {}
        for key, row in brokers.items():
            if not isinstance(row, dict):
                continue
            try:
                cap = _parse_capabilities_row(str(key), row)
                self._capabilities[cap.name] = cap
            except Exception as exc:  # noqa: BLE001
                if self._logger:
                    self._logger.warning("capability_registry | skip {} | {}", key, exc)

    def register_capabilities(self, capabilities: BrokerCapabilities) -> None:
        self._capabilities[capabilities.name] = capabilities

    def get(self, broker_name: str) -> Optional[BrokerCapabilities]:
        return self._capabilities.get(broker_name.strip().lower())

    def all(self) -> List[BrokerCapabilities]:
        return list(self._capabilities.values())

    def get_spot_brokers(self, symbol: str) -> List[BrokerCapabilities]:
        sym = symbol.strip().upper()
        return [b for b in self._capabilities.values() if b.supports_spot and sym in b.supported_symbols]

    def get_perp_brokers(self, symbol: str) -> List[BrokerCapabilities]:
        sym = symbol.strip().upper()
        return [
            b for b in self._capabilities.values() if b.supports_perpetuals and sym in b.supported_symbols
        ]

    def get_shortable_brokers(self, symbol: str) -> List[BrokerCapabilities]:
        sym = symbol.strip().upper()
        return [b for b in self._capabilities.values() if b.supports_shorting and sym in b.supported_symbols]

    @staticmethod
    def filter_by_liquidity(
        brokers: Iterable[BrokerCapabilities],
        min_liquidity: Decimal,
    ) -> List[BrokerCapabilities]:
        return [b for b in brokers if b.liquidity_score >= min_liquidity]

    @staticmethod
    def filter_by_latency(
        brokers: Iterable[BrokerCapabilities],
        max_latency_ms: int,
    ) -> List[BrokerCapabilities]:
        return [b for b in brokers if b.latency_ms <= max_latency_ms]
