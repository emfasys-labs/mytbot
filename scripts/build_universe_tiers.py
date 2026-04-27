#!/usr/bin/env python3
"""
Offline build: correlation clusters + universe_intelligence.json.

Requires ``config/universe_selection.enabled: true`` and existing
``data/runtime/universe_tiers.json`` (from dynamic ranking / loop).

Does not place orders or touch RiskEngine.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from data.universe_tiers import load_universe_tiers  # noqa: E402
from data.yfinance_fetch import fetch_history  # noqa: E402
from universe.clustering import cluster_by_correlation  # noqa: E402
from universe.correlation_graph import correlation_matrix  # noqa: E402
from universe.persistence import merge_cluster_payload, save_intelligence_state  # noqa: E402
from universe.representative_selector import select_representatives  # noqa: E402
from universe.snapshot_service import load_universe_selection_config  # noqa: E402
from universe.universe_tiers import UniverseIntelligenceState  # noqa: E402


def main() -> int:
    cfg = load_universe_selection_config(ROOT / "config" / "universe_selection.yaml")
    if not cfg.get("enabled"):
        print("universe_selection.enabled is false — enable in config/universe_selection.yaml to build.")
        return 0

    tiers = load_universe_tiers(ROOT / "data" / "runtime" / "universe_tiers.json")
    if not tiers:
        print("Missing data/runtime/universe_tiers.json — run the trading loop / ranking first.")
        return 1

    cap = int(cfg.get("cluster_max_symbols", 120))
    thresh = float(cfg.get("correlation_cluster_threshold", 0.88))
    symbols = list(dict.fromkeys(list(tiers.core) + list(tiers.scan)))[:cap]
    scores = dict(tiers.scores)

    price_series: dict[str, list[float]] = {}
    for sym in symbols:
        try:
            df = fetch_history(sym.strip(), period="3mo", interval="1d")
            if df is not None and not df.empty and "Close" in df.columns:
                price_series[sym.upper()] = [float(x) for x in df["Close"].tolist() if str(x) != "nan"]
        except Exception as exc:  # noqa: BLE001
            print(f"skip {sym}: {exc}")

    if len(price_series) < 5:
        print(f"Too few price series for clustering ({len(price_series)}).")
        return 1

    ordered = list(price_series.keys())
    mat, used = correlation_matrix(ordered, price_series)
    clusters_idx = cluster_by_correlation(used, mat, threshold=thresh)
    reps = select_representatives(clusters_idx, used, scores)
    clusters = merge_cluster_payload(used, mat, clusters_idx, reps)

    rep_set = {v.upper() for v in reps.values()}
    cold = [s for s in ordered if s.upper() not in rep_set]

    state = UniverseIntelligenceState(
        candidate_count=len(ordered) * 25,
        cold_scan=cold,
        active_eval=list(dict.fromkeys(list(tiers.scan) + list(tiers.core))),
        core=sorted(rep_set),
        clusters=clusters,
        promotions=list(cfg.get("seed_promotions") or []),
        last_full_cluster_at=datetime.now(timezone.utc).isoformat(),
    )

    out_path = ROOT / str((cfg.get("persistence") or {}).get("intelligence_json", "data/runtime/universe_intelligence.json"))
    save_intelligence_state(state, out_path)
    print(f"wrote {out_path} | clusters={len(clusters)} symbols_scored={len(used)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
