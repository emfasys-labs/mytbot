"""Phase E learned cross-asset relational demand graph.

Learns lagged relationships from historical close prices and evaluates them as
a shadow demand component. It is deliberately JSON-only and dependency-light so
the live demand engine can read an artifact without making a training stack a
runtime dependency.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

import pandas as pd


@dataclass(frozen=True)
class RelationalEdge:
    source: str
    target: str
    weight: float
    lag_correlation: float
    same_bar_correlation: float
    confidence: float
    observations: int


@dataclass(frozen=True)
class RelationalDemandGraphArtifact:
    version: str
    timeframe: str
    symbols: list[str]
    edges: list[RelationalEdge]
    metadata: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "timeframe": self.timeframe,
            "symbols": list(self.symbols),
            "edges": [asdict(e) for e in self.edges],
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "RelationalDemandGraphArtifact":
        return cls(
            version=str(raw.get("version") or "phase_e_relational_v1"),
            timeframe=str(raw.get("timeframe") or "1h"),
            symbols=[str(s) for s in (raw.get("symbols") or [])],
            edges=[RelationalEdge(**e) for e in (raw.get("edges") or []) if isinstance(e, dict)],
            metadata=dict(raw.get("metadata") or {}),
        )


def _returns(close: pd.DataFrame) -> pd.DataFrame:
    return close.sort_index().pct_change().replace([float("inf"), float("-inf")], pd.NA).dropna(how="all")


def learn_relational_edges(
    close: pd.DataFrame,
    *,
    min_overlap: int = 200,
    min_abs_lag_corr: float = 0.15,
    max_edges: int = 80,
) -> list[RelationalEdge]:
    """Learn directed lead/lag edges: source return at t-1 vs target return at t."""
    rets = _returns(close)
    symbols = [str(c) for c in rets.columns]
    edges: list[RelationalEdge] = []
    for src in symbols:
        src_lag = rets[src].shift(1)
        for dst in symbols:
            if src == dst:
                continue
            pair = pd.concat([src_lag, rets[dst]], axis=1).dropna()
            if len(pair) < min_overlap:
                continue
            lag_corr = float(pair.iloc[:, 0].corr(pair.iloc[:, 1]))
            if pd.isna(lag_corr) or abs(lag_corr) < min_abs_lag_corr:
                continue
            same = pd.concat([rets[src], rets[dst]], axis=1).dropna()
            same_corr = float(same.iloc[:, 0].corr(same.iloc[:, 1])) if len(same) >= min_overlap else 0.0
            if pd.isna(same_corr):
                same_corr = 0.0
            confidence = min(1.0, abs(lag_corr) * min(1.0, len(pair) / max(float(min_overlap), 1.0)))
            edges.append(
                RelationalEdge(
                    source=src,
                    target=dst,
                    weight=max(-1.0, min(1.0, lag_corr)),
                    lag_correlation=lag_corr,
                    same_bar_correlation=same_corr,
                    confidence=confidence,
                    observations=int(len(pair)),
                )
            )
    return sorted(edges, key=lambda e: (abs(e.weight) * e.confidence, e.observations), reverse=True)[:max_edges]


def build_relational_artifact(
    close: pd.DataFrame,
    *,
    timeframe: str = "1h",
    version: str = "phase_e_relational_v1",
    min_overlap: int = 200,
    min_abs_lag_corr: float = 0.15,
    max_edges: int = 80,
) -> RelationalDemandGraphArtifact:
    edges = learn_relational_edges(
        close,
        min_overlap=min_overlap,
        min_abs_lag_corr=min_abs_lag_corr,
        max_edges=max_edges,
    )
    return RelationalDemandGraphArtifact(
        version=version,
        timeframe=timeframe,
        symbols=[str(c) for c in close.columns],
        edges=edges,
        metadata={
            "min_overlap": int(min_overlap),
            "min_abs_lag_corr": float(min_abs_lag_corr),
            "max_edges": int(max_edges),
            "bar_count": int(len(close)),
        },
    )


def evaluate_relational_shadow(
    artifact: RelationalDemandGraphArtifact,
    feature_map: dict[str, pd.DataFrame],
    *,
    scale: float = 30.0,
) -> dict[str, float | int | bool]:
    """Evaluate learned graph against current two-bar feature frames."""
    returns: dict[str, float] = {}
    for sym, df in (feature_map or {}).items():
        key = str(sym).strip().upper()
        if df is None or df.empty or "close" not in df.columns or len(df) < 2:
            continue
        try:
            c0 = float(df["close"].iloc[-2])
            c1 = float(df["close"].iloc[-1])
        except Exception:  # noqa: BLE001
            continue
        if c0 > 0:
            returns[key] = (c1 - c0) / c0
    terms: list[float] = []
    confs: list[float] = []
    for edge in artifact.edges:
        src = edge.source.upper()
        if src not in returns:
            continue
        terms.append(float(edge.weight) * returns[src] * float(edge.confidence))
        confs.append(float(edge.confidence))
    raw = sum(terms) / len(terms) if terms else 0.0
    score = max(-1.0, min(1.0, raw * float(scale)))
    return {
        "learned_cross_asset_shadow_used": bool(terms),
        "learned_cross_asset_shadow_score": float(score),
        "learned_cross_asset_shadow_edges_used": int(len(terms)),
        "learned_cross_asset_shadow_edge_count": int(len(artifact.edges)),
        "learned_cross_asset_shadow_confidence": float(sum(confs) / len(confs)) if confs else 0.0,
    }
