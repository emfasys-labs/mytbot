"""
scripts/evaluate_microstructure.py
====================================
Wave 10 — train + evaluate an LOB imbalance forecaster from a CSV of
snapshots and forward returns.

Expected CSV columns (one row per snapshot):
    timestamp, symbol, asset_class,
    bid_price, bid_qty, ask_price, ask_qty,
    bid2_price, bid2_qty, ask2_price, ask2_qty, ...
    forward_return

Quick research utility — production training uses a real broker feed.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from data.orderbook_features import OrderbookLevel, OrderbookSnapshot  # noqa: E402
from models.microstructure import (  # noqa: E402
    stack_lob_features,
    train_imbalance_forecaster,
)


def _row_to_snapshot(row: pd.Series) -> OrderbookSnapshot:
    bids = []
    asks = []
    for k in range(1, 11):
        bp = row.get(f"bid{k}_price") if k > 1 else row.get("bid_price")
        bq = row.get(f"bid{k}_qty") if k > 1 else row.get("bid_qty")
        ap = row.get(f"ask{k}_price") if k > 1 else row.get("ask_price")
        aq = row.get(f"ask{k}_qty") if k > 1 else row.get("ask_qty")
        if bp is None or pd.isna(bp):
            break
        bids.append(OrderbookLevel(price=Decimal(str(bp)), quantity=Decimal(str(bq or 0))))
        if ap is not None and not pd.isna(ap):
            asks.append(OrderbookLevel(price=Decimal(str(ap)), quantity=Decimal(str(aq or 0))))
    ts = pd.to_datetime(row["timestamp"], utc=True).to_pydatetime()
    return OrderbookSnapshot(
        symbol=str(row.get("symbol", "")),
        bids=tuple(bids),
        asks=tuple(asks),
        timestamp=ts,
        asset_class=str(row.get("asset_class", "crypto")),
    )


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Evaluate microstructure forecaster (Wave 10)")
    p.add_argument("--snapshots", required=True)
    p.add_argument("--depth", type=int, default=5)
    p.add_argument("--out", default=None, help="optional pickle path for the trained artefact")
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    df = pd.read_csv(args.snapshots)
    snaps = [_row_to_snapshot(row) for _, row in df.iterrows()]
    forward = list(df.get("forward_return", pd.Series(dtype=float)))
    if not forward:
        raise SystemExit("CSV missing 'forward_return' column")

    ds = stack_lob_features(snaps, forward, depth=args.depth)
    if len(ds.X) < 50:
        print(f"warning: only {len(ds.X)} usable rows after feature gating")
    art, report = train_imbalance_forecaster(dataset=ds)
    print(report.summary())
    if args.out:
        art.save(args.out)
        print(f"artefact written to: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
