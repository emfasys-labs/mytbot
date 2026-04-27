"""
Universe Intelligence Layer — correlation-aware tiering and promotion rules.

Feeds the data pipeline and UI; does not execute orders or bypass risk.
"""

from __future__ import annotations

from universe.snapshot_service import build_universe_snapshot_dict, load_universe_selection_config

__all__ = [
    "build_universe_snapshot_dict",
    "load_universe_selection_config",
]
