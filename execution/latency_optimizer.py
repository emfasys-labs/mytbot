from __future__ import annotations

import time
from typing import Any


class LatencyOptimizer:
    """Lightweight RTT cache; pair with ``LatencyPredictor`` for trending behaviour."""

    def __init__(self, logger: Any | None = None) -> None:
        self._logger = logger
        self._latency_cache: dict[str, float] = {}

    async def measure_latency(self, broker: Any) -> float:
        name = str(getattr(broker, "broker_name", "unknown")).strip().lower()
        ping = getattr(broker, "ping", None)
        start = time.perf_counter()
        if callable(ping):
            try:
                res = ping()
                if hasattr(res, "__await__"):
                    await res
            except Exception:  # noqa: BLE001
                pass
        else:
            try:
                connected = await broker.is_connected()
                if not connected:
                    await broker.connect()
            except Exception:  # noqa: BLE001
                pass
        latency_ms = (time.perf_counter() - start) * 1000.0
        self._latency_cache[name] = latency_ms
        return latency_ms

    def get_latency(self, broker_name: str) -> float:
        return float(self._latency_cache.get(broker_name.strip().lower(), 999.0))

    def is_too_slow(self, broker_name: str, max_latency_ms: float) -> bool:
        return self.get_latency(broker_name) > max_latency_ms
