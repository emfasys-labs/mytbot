from __future__ import annotations

from collections import deque


class LatencyPredictor:
    """Rolling latency history per venue for scoring and degradation detection."""

    def __init__(self, window: int = 50) -> None:
        self._history: dict[str, deque[float]] = {}
        self._window = max(5, int(window))

    def record(self, broker_name: str, latency_ms: float) -> None:
        name = broker_name.strip().lower()
        if name not in self._history:
            self._history[name] = deque(maxlen=self._window)
        self._history[name].append(float(latency_ms))

    def get_average(self, broker_name: str) -> float:
        values = list(self._history.get(broker_name.strip().lower(), []))
        if not values:
            return 999.0
        return sum(values) / len(values)

    def get_trend(self, broker_name: str) -> float:
        values = list(self._history.get(broker_name.strip().lower(), []))
        if len(values) < 5:
            return 0.0
        return values[-1] - values[0]

    def is_degrading(self, broker_name: str, threshold: float = 50.0) -> bool:
        return self.get_trend(broker_name) > threshold
