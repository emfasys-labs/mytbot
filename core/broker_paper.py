"""Brokers without exchange-native paper accounts.

Simulated fills are persisted via ``PositionLog``; reconciliation treats those
rows as authoritative in ``paper_mode`` instead of live ``get_positions()``.
"""

from __future__ import annotations

# Keep in sync with execution-layer reconciliation (``ExecutionEngine``).
NO_NATIVE_PAPER_POSITION_BROKERS: frozenset[str] = frozenset({"kraken", "binance", "bybit"})
