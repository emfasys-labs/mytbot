from __future__ import annotations


class VenueHealthMonitor:
    """Counts API/order anomalies and stale-quote flags per broker name."""

    def __init__(self) -> None:
        self._errors: dict[str, int] = {}
        self._stale_flags: dict[str, bool] = {}

    def record_error(self, broker: str) -> None:
        b = broker.strip().lower()
        self._errors[b] = self._errors.get(b, 0) + 1

    def reset_errors(self, broker: str) -> None:
        self._errors.pop(broker.strip().lower(), None)

    def is_unhealthy(self, broker: str, max_errors: int = 5) -> bool:
        return self._errors.get(broker.strip().lower(), 0) > max_errors

    def mark_stale(self, broker: str, stale: bool = True) -> None:
        self._stale_flags[broker.strip().lower()] = stale

    def is_stale(self, broker: str) -> bool:
        return bool(self._stale_flags.get(broker.strip().lower(), False))
