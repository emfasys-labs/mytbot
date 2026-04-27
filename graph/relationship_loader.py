"""
graph/relationship_loader.py
==============================
Wave 7 — pluggable loader for symbol-relationship data.

The existing ``graph/engine.DependencyGraphEngine`` already provides a
runtime evaluator over a configured dependency graph. This module
gives the operator a thin, well-typed surface for loading the
underlying relationship table from heterogeneous sources (YAML file,
DB query result, in-memory dict) without forcing the engine to know
about each shape.

A ``RelationshipLoader`` returns a list of ``Relationship`` records
that the engine (or the fusion graph context builder) can then index.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Mapping, Optional

import yaml


@dataclass(frozen=True)
class Relationship:
    """One directed edge in the dependency graph."""

    upstream_symbol: str
    downstream_symbol: str
    direction: str = "co_move"          # "co_move" | "inverse" | "lead_lag"
    static_confidence: float = 0.5      # 0..1
    expected_lag_hours: int = 0
    asset_class_downstream: Optional[str] = None
    notes: str = ""


# ── loaders ────────────────────────────────────────────────────────────────


def load_relationships_from_dict(raw: Optional[Mapping[str, object]]) -> list[Relationship]:
    """Parse a dict-of-dicts shape::

        relationships:
          - upstream_symbol: SPY
            downstream_symbol: AAPL
            direction: co_move
            static_confidence: 0.7
            expected_lag_hours: 0
            asset_class_downstream: equity
    """
    if not raw:
        return []
    items = raw.get("relationships") or []
    out: list[Relationship] = []
    for item in items:
        if not isinstance(item, Mapping):
            continue
        try:
            out.append(
                Relationship(
                    upstream_symbol=str(item["upstream_symbol"]),
                    downstream_symbol=str(item["downstream_symbol"]),
                    direction=str(item.get("direction", "co_move")),
                    static_confidence=float(item.get("static_confidence", 0.5)),
                    expected_lag_hours=int(item.get("expected_lag_hours", 0)),
                    asset_class_downstream=(
                        str(item["asset_class_downstream"])
                        if item.get("asset_class_downstream")
                        else None
                    ),
                    notes=str(item.get("notes", "")),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return out


def load_relationships_from_yaml(path: Path | str) -> list[Relationship]:
    p = Path(path)
    if not p.exists():
        return []
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return []
    return load_relationships_from_dict(raw)


# ── lookup helpers ─────────────────────────────────────────────────────────


@dataclass
class RelationshipIndex:
    """Indexed lookup over a ``Relationship`` list."""

    relationships: list[Relationship] = field(default_factory=list)
    _by_upstream: dict[str, list[Relationship]] = field(default_factory=dict)
    _by_downstream: dict[str, list[Relationship]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._by_upstream = {}
        self._by_downstream = {}
        for r in self.relationships:
            self._by_upstream.setdefault(r.upstream_symbol.upper(), []).append(r)
            self._by_downstream.setdefault(r.downstream_symbol.upper(), []).append(r)

    def downstream_for(self, symbol: str) -> list[Relationship]:
        return self._by_upstream.get((symbol or "").strip().upper(), [])

    def upstream_for(self, symbol: str) -> list[Relationship]:
        return self._by_downstream.get((symbol or "").strip().upper(), [])

    def affected_symbols(self, upstream_symbol: str) -> tuple[str, ...]:
        rels = self.downstream_for(upstream_symbol)
        return tuple(r.downstream_symbol for r in rels)

    def affected_asset_classes(self, upstream_symbol: str) -> tuple[str, ...]:
        rels = self.downstream_for(upstream_symbol)
        seen: list[str] = []
        for r in rels:
            ac = r.asset_class_downstream
            if ac and ac not in seen:
                seen.append(ac)
        return tuple(seen)
