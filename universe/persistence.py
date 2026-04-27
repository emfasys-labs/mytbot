from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from universe.universe_tiers import UniverseIntelligenceState

DEFAULT_INTELLIGENCE_PATH = Path("data/runtime/universe_intelligence.json")


def load_intelligence_state(path: Path | None = None) -> UniverseIntelligenceState | None:
    p = path or DEFAULT_INTELLIGENCE_PATH
    if not p.is_file():
        return None
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return None
        return UniverseIntelligenceState.from_json_obj(raw)
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None


def save_intelligence_state(state: UniverseIntelligenceState, path: Path | None = None) -> None:
    p = path or DEFAULT_INTELLIGENCE_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state.to_json_obj(), indent=2), encoding="utf-8")


def merge_cluster_payload(
    symbols: list[str],
    corr: list[list[float]],
    clusters_idx: list[list[int]],
    representatives: dict[int, str],
) -> list[dict[str, Any]]:
    from universe.representative_selector import cluster_avg_abs_correlation

    out: list[dict[str, Any]] = []
    for ci, idxs in enumerate(clusters_idx):
        syms = [symbols[i] for i in idxs if 0 <= i < len(symbols)]
        rep = representatives.get(ci, syms[0] if syms else "")
        avg_c = cluster_avg_abs_correlation(idxs, corr)
        out.append(
            {
                "id": ci,
                "members": syms,
                "representative": rep,
                "avg_abs_correlation": round(avg_c, 4),
                "member_count": len(syms),
            }
        )
    return out
