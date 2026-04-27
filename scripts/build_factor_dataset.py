"""
scripts/build_factor_dataset.py
================================
Wave 3 — assemble a cross-sectional factor snapshot from a directory of
per-symbol OHLCV CSVs (and optional fundamentals JSON files), writing
out a single CSV with one row per symbol and the full factor block.

Inputs:
    --prices  <dir>   Directory of <symbol>.csv files with a datetime
                      index and a 'close' column.
    --fundamentals <dir>  Optional directory of <symbol>.json files
                          containing the fundamentals dict expected by
                          ``data.fundamental_features``.
    --asset-classes  <yaml>  Optional YAML mapping symbol → asset class.
    --benchmark <symbol>  Symbol whose CSV is used as the beta benchmark.
    --out <csv>       Output path.

Read-only / research script — no DB writes.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

from data.factor_features import build_price_factors  # noqa: E402
from data.fundamental_features import build_fundamental_factors  # noqa: E402


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Build factor snapshot CSV (Wave 3)")
    p.add_argument("--prices", required=True)
    p.add_argument("--fundamentals", default=None)
    p.add_argument("--asset-classes", default=None)
    p.add_argument("--benchmark", default=None)
    p.add_argument("--out", required=True)
    return p.parse_args()


def _load_asset_class_map(path: str | None) -> dict[str, str]:
    if not path:
        return {}
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return {str(k): str(v) for k, v in raw.items()}


def _load_close(p: Path) -> pd.Series:
    df = pd.read_csv(p, index_col=0, parse_dates=True)
    return df["close"].astype(float)


def _load_fundamentals(p: Path | None, symbol: str) -> dict | None:
    if p is None:
        return None
    f = p / f"{symbol}.json"
    if not f.exists():
        return None
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def main() -> int:
    args = _parse_args()
    prices_dir = Path(args.prices)
    fund_dir = Path(args.fundamentals) if args.fundamentals else None
    classes = _load_asset_class_map(args.asset_classes)

    benchmark = None
    if args.benchmark:
        bp = prices_dir / f"{args.benchmark}.csv"
        if bp.exists():
            benchmark = _load_close(bp)

    rows = []
    for csv in sorted(prices_dir.glob("*.csv")):
        symbol = csv.stem
        close = _load_close(csv)
        price_feats = build_price_factors(close, benchmark_close=benchmark)
        fund = _load_fundamentals(fund_dir, symbol)
        fund_feats = build_fundamental_factors(fund)
        merged = {"symbol": symbol, "asset_class": classes.get(symbol, "other")}
        merged.update(price_feats)
        merged.update(fund_feats)
        rows.append(merged)

    pd.DataFrame(rows).to_csv(args.out, index=False)
    print(f"factor snapshot written: {args.out} ({len(rows)} symbols)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
