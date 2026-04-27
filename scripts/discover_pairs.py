"""
scripts/discover_pairs.py
==========================
Wave 5 — discover candidate pairs from a directory of per-symbol OHLCV
CSVs.

Usage:
    python scripts/discover_pairs.py \\
        --prices data/research/prices \\
        --config config/pairs_trading.yaml \\
        --top 20
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from models.pairs.universe import discover_pair_candidates  # noqa: E402


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Discover pairs (Wave 5)")
    p.add_argument("--prices", required=True, help="dir of <symbol>.csv with 'close' column")
    p.add_argument("--config", default="config/pairs_trading.yaml")
    p.add_argument("--top", type=int, default=20)
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    cfg_raw = yaml.safe_load(Path(args.config).read_text(encoding="utf-8")) or {}
    udcfg = (cfg_raw.get("universe_discovery") or {})

    prices: dict[str, pd.Series] = {}
    for csv in sorted(Path(args.prices).glob("*.csv")):
        df = pd.read_csv(csv, index_col=0, parse_dates=True)
        if "close" not in df.columns:
            continue
        prices[csv.stem] = df["close"].astype(float)
    if len(prices) < 2:
        print("not enough symbols to form pairs")
        return 1

    candidates = discover_pair_candidates(
        prices,
        min_correlation=float(udcfg.get("min_correlation", 0.6)),
        max_p_value=float(udcfg.get("max_p_value", 0.05)),
        max_half_life_bars=float(udcfg.get("max_half_life_bars", 200.0)),
        top_n=args.top,
    )
    if not candidates:
        print("no candidates passed the gates")
        return 0
    print(f"top {len(candidates)} pair candidates:")
    for c in candidates:
        print(
            f"  {c.leg_a:<10} | {c.leg_b:<10}  corr={c.correlation:+.3f}  "
            f"adf={c.eg_result.adf_stat:+.3f}  p~{c.eg_result.p_value_estimate:.3f}  "
            f"hl={c.half_life_bars:.1f}  score={c.composite_score:.3f}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
