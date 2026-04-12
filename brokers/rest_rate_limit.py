"""
brokers/rest_rate_limit.py
===========================
Proactive spacing between REST calls for exchange SDKs (Binance, Bybit, etc.)
to reduce 429 / rate-limit responses before they occur.

Configure with ``{PREFIX}_REST_MIN_INTERVAL_SEC`` (e.g. ``BINANCE_REST_MIN_INTERVAL_SEC``)
or pass ``rest_min_interval_sec`` into the adapter constructor.
"""

from __future__ import annotations

import asyncio
import os
import time


def _parse_env_interval(env_name: str, default_sec: float) -> float:
    raw = os.getenv(env_name, "").strip()
    if not raw:
        return default_sec
    try:
        return max(0.0, float(raw))
    except ValueError:
        return default_sec


class AsyncRestGap:
    """Enforce a minimum gap between successive blocking REST calls (asyncio-safe)."""

    def __init__(self, min_interval_sec: float) -> None:
        self._min = max(0.0, float(min_interval_sec))
        self._next_allowed: float | None = None
        self._lock = asyncio.Lock()

    @classmethod
    def from_env(cls, prefix: str, *, default_seconds: float = 0.055) -> AsyncRestGap:
        """Read ``{PREFIX}_REST_MIN_INTERVAL_SEC`` (e.g. prefix ``BINANCE``)."""
        sec = _parse_env_interval(f"{prefix.upper()}_REST_MIN_INTERVAL_SEC", default_seconds)
        return cls(sec)

    async def wait(self) -> None:
        if self._min <= 0:
            return
        async with self._lock:
            now = time.monotonic()
            if self._next_allowed is not None and now < self._next_allowed:
                await asyncio.sleep(self._next_allowed - now)
                now = time.monotonic()
            self._next_allowed = now + self._min
